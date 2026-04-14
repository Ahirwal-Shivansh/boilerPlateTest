"""
Simple Auto-Updater Module for EG Agent

Checks for updates when WebSocket connection is established.
Downloads and applies updates when version mismatch is detected.

Note: Set AGENT_VERSION in your .env file to specify your application version.
Example: AGENT_VERSION=1.0.0
"""

import os
import sys
import hashlib
import tempfile
import platform
import subprocess
import shutil
from pathlib import Path
from typing import Optional, Callable

from eg_agent.log_config import logger as base_logger

logger = base_logger.getChild("auto_updater")


class AutoUpdater:
    """
    Simple auto-updater that checks for updates on WebSocket connection.

    Flow:
    1. On WS connect -> send version-check with current version
    2. Receive version-check-response with latest version
    3. If versions differ -> send download-request
    4. Receive download-response with URL
    5. Download, verify, apply update
    """

    def __init__(
        self,
        current_version: str,
        app_slug: str = "backup-verification",
        shutdown_callback: Optional[Callable[[], None]] = None
    ):
        self.current_version = current_version
        self.app_slug = app_slug
        self.platform = self._detect_platform()
        self.latest_version: Optional[str] = None
        self.download_url: Optional[str] = None
        self.checksum: Optional[str] = None
        self.file_size: int = 0
        self.is_required: bool = False
        self._ws_send: Optional[Callable] = None
        self._shutdown_callback = shutdown_callback

        # Download directory
        self.download_dir = Path(tempfile.gettempdir()) / "eg-agent-updates"
        self.download_dir.mkdir(parents=True, exist_ok=True)

        logger.info(
            "AutoUpdater: version=%s, platform=%s",
            self.current_version, self.platform
        )

    @staticmethod
    def _detect_platform() -> str:
        """Detect current platform."""
        system = platform.system().lower()
        if system == "darwin":
            machine = (platform.machine() or "").lower()
            # Apple Silicon typically reports arm64/aarch64; Intel reports x86_64.
            if machine in ("arm64", "aarch64"):
                return "macos-silicon"
            return "macos-intel"
        elif system == "windows":
            return "windows"
        return "linux"

    def set_ws_send(self, ws_send: Callable):
        """Set the WebSocket send function."""
        self._ws_send = ws_send

    def needs_update(self) -> bool:
        """Check if update is needed (versions don't match)."""
        if not self.latest_version:
            return False
        return self.current_version != self.latest_version

    def is_rollback(self) -> bool:
        """Check if this is a rollback (current > latest)."""
        if not self.latest_version:
            return False
        return self._compare_versions(
            self.current_version, self.latest_version
        ) > 0

    @staticmethod
    def _compare_versions(v1: str, v2: str) -> int:
        """Compare versions. Returns: -1 if v1<v2, 0 if equal, 1 if v1>v2."""
        def parse(v):
            return tuple(int(x) for x in v.lstrip("v").split(".")[:3])
        try:
            p1, p2 = parse(v1), parse(v2)
            if p1 < p2:
                return -1
            elif p1 > p2:
                return 1
            return 0
        except (ValueError, IndexError):
            return 0

    # =========================================================================
    # MESSAGE CREATION
    # =========================================================================

    def create_version_check_message(self) -> dict:
        """Create version-check message to send on connection."""
        return {
            "type": "version-check",
            "app_slug": self.app_slug,
            "platform": self.platform,
            "version": self.current_version,
        }

    def create_download_request_message(self) -> dict:
        """Create download-request message."""
        return {
            "type": "download-request",
            "app_slug": self.app_slug,
            "platform": self.platform,
        }

    # =========================================================================
    # RESPONSE HANDLERS
    # =========================================================================

    def handle_version_check_response(self, response: dict) -> bool:
        """
        Handle version-check-response from server.

        Returns True if update/rollback is needed.
        """
        if not response.get("success", False):
            logger.warning(
                "Version check failed: %s", response.get("message")
            )
            return False

        self.latest_version = response.get("latest_version")
        self.is_required = response.get("is_required", False)

        # Extract release info if available
        release = response.get("release", {})
        if release:
            self.checksum = release.get("checksum", "")
            self.file_size = release.get("size", 0)

        if not self.latest_version:
            logger.info("No version info received")
            return False

        if self.needs_update():
            if self.is_rollback():
                logger.info(
                    "Rollback needed: %s -> %s",
                    self.current_version, self.latest_version
                )
            else:
                logger.info(
                    "Update available: %s -> %s",
                    self.current_version, self.latest_version
                )
            return True
        else:
            logger.info("Already on latest version: %s", self.current_version)
            return False

    def handle_download_response(self, response: dict) -> bool:
        """
        Handle download-response from server.

        Returns True if download URL received.
        """
        if not response.get("success", False):
            logger.error(
                "Download request failed: %s", response.get("message")
            )
            return False

        self.download_url = response.get("download_url")
        self.checksum = response.get("checksum", self.checksum)
        self.file_size = response.get("size", self.file_size)

        if self.download_url:
            logger.info(
                "Download URL received, expires in %ds",
                response.get("expires_in", 0)
            )
            return True
        return False

    # =========================================================================
    # DOWNLOAD & VERIFY
    # =========================================================================

    async def download_update(self) -> Optional[str]:
        """
        Download the update file.

        Returns path to downloaded file or None on failure.
        """
        import urllib.request
        import urllib.error

        if not self.download_url:
            logger.error("No download URL available")
            return None

        # Extract original filename from the S3 URL (strip query params)
        from urllib.parse import urlparse, unquote
        url_path = urlparse(self.download_url).path
        filename = unquote(url_path.rsplit("/", 1)[-1]) if "/" in url_path else url_path
        if not filename:
            ext = ".pkg" if self.platform.startswith("macos") else ".msi"
            filename = f"eg-agent-{self.latest_version}-{self.platform}{ext}"
        download_path = self.download_dir / filename

        try:
            logger.info("Downloading update to %s", download_path)

            req = urllib.request.Request(self.download_url)
            with urllib.request.urlopen(req, timeout=300) as response:
                with open(download_path, 'wb') as f:
                    total = 0
                    while True:
                        chunk = response.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)
                        total += len(chunk)

            logger.info("Download complete: %d bytes", total)
            return str(download_path)

        except urllib.error.URLError as e:
            logger.error("Download failed: %s", e)
            return None
        except Exception as e:
            logger.error("Download error: %s", e)
            return None

    def verify_checksum(self, file_path: str) -> bool:
        """Verify file checksum."""
        if not self.checksum:
            logger.warning("No checksum to verify, skipping")
            return True

        expected = self.checksum
        if expected.startswith("sha256:"):
            expected = expected[7:]

        try:
            sha256 = hashlib.sha256()
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    sha256.update(chunk)

            actual = sha256.hexdigest()
            if actual.lower() == expected.lower():
                logger.info("Checksum verified")
                return True
            else:
                logger.error("Checksum mismatch!")
                return False
        except Exception as e:
            logger.error("Checksum verification error: %s", e)
            return False

    # =========================================================================
    # APPLY UPDATE
    # =========================================================================

    async def apply_update(self, file_path: str) -> bool:
        """Apply the downloaded update."""
        try:
            if self.platform.startswith("macos"):
                return await self._apply_macos(file_path)
            elif self.platform == "windows":
                return await self._apply_windows(file_path)
            else:
                logger.error("Unsupported platform: %s", self.platform)
                return False
        except Exception as e:
            logger.error("Failed to apply update: %s", e)
            return False

    async def _apply_macos(self, file_path: str) -> bool:
        """Apply update on macOS."""
        logger.info("Applying macOS update: %s", file_path)

        if not getattr(sys, 'frozen', False):
            logger.info("Dev mode: skipping actual update")
            return True

        if file_path.endswith(".pkg"):
            try:
                logger.info("Preparing PKG update: %s", file_path)

                logger.info("Exiting application to allow PKG installation...")

                logger.info("Launching PKG installer...")
                subprocess.Popen(["open", file_path])

                import time
                time.sleep(1)

                if self._shutdown_callback:
                    logger.info(
                        "Calling shutdown callback to exit gracefully...")
                    try:
                        self._shutdown_callback()
                    except Exception as e:
                        logger.warning("Shutdown callback failed: %s", e)
                        sys.exit(0)
                else:
                    logger.warning("No shutdown callback set, forcing exit...")
                    sys.exit(0)

                return True

            except Exception as e:
                logger.error("macOS PKG update failed: %s", e)
                return False

        logger.error("Unsupported file format for macOS update: %s", file_path)
        return False

    async def _apply_windows(self, file_path: str) -> bool:
        """Apply update on Windows."""
        logger.info("Applying Windows update: %s", file_path)

        if not getattr(sys, 'frozen', False):
            logger.info("Dev mode: skipping actual update")
            return True

        try:
            if file_path.endswith(".msi"):
                import time

                agent_pid = os.getpid()
                msi_path = file_path.replace('"', '""')

                # Use a VBScript to reliably trigger UAC elevation.
                # Shell.Application.ShellExecute "runas" works even from
                # non-interactive / detached processes, unlike PowerShell
                # Start-Process -Verb RunAs which needs an interactive desktop.
                update_script = self.download_dir / "eg_update.vbs"
                update_script.write_text(
                    f'Set wmi = GetObject("winmgmts:\\\\.\\root\\cimv2")\n'
                    f'Do\n'
                    f'  Set procs = wmi.ExecQuery("SELECT * FROM Win32_Process WHERE ProcessId = {agent_pid}")\n'
                    f'  If procs.Count = 0 Then Exit Do\n'
                    f'  WScript.Sleep 1000\n'
                    f'Loop\n'
                    f'WScript.Sleep 2000\n'
                    f'Set shell = CreateObject("Shell.Application")\n'
                    f'shell.ShellExecute "msiexec", "/i ""{msi_path}"" /qn /norestart", "", "runas", 0\n',
                    encoding="utf-8",
                )

                subprocess.Popen(
                    ["wscript", str(update_script)],
                    creationflags=subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NEW_PROCESS_GROUP,
                )
                logger.info(
                    "MSI update VBS launched (waiting for pid %d to exit)", agent_pid
                )

                time.sleep(1)

                if self._shutdown_callback:
                    try:
                        self._shutdown_callback()
                    except Exception as e:
                        logger.warning("Shutdown callback failed: %s", e)
                        sys.exit(0)
                else:
                    sys.exit(0)
                return True

            elif file_path.endswith(".exe"):
                subprocess.Popen(
                    [file_path, "/S"],
                    creationflags=subprocess.DETACHED_PROCESS,
                )
                logger.info("EXE installer launched")
                return True

            elif file_path.endswith(".zip"):
                current_exe = Path(sys.executable)
                extract_dir = self.download_dir / "extracted"
                shutil.unpack_archive(file_path, extract_dir)

                # Create batch script for deferred update
                batch = self.download_dir / "update.bat"
                batch.write_text(
                    f'''@echo off
                    timeout /t 2 /nobreak > nul
                    copy /y "{extract_dir}\\*" "{current_exe.parent}"
                    start "" "{current_exe}"
                    del "%~f0"
                    '''
                )
                subprocess.Popen(
                    ["cmd", "/c", str(batch)],
                    creationflags=subprocess.DETACHED_PROCESS,
                )
                logger.info("Update scheduled")
                return True

        except Exception as e:
            logger.error("Windows update failed: %s", e)
            return False
        return False

    def cleanup(self):
        """Clean up temporary files."""
        try:
            if self.download_dir.exists():
                for item in self.download_dir.iterdir():
                    if item.is_file():
                        item.unlink()
                    elif item.is_dir():
                        shutil.rmtree(item)
            logger.info("Cleanup done")
        except Exception as e:
            logger.error("Cleanup failed: %s", e)


# Global instance
_updater: Optional[AutoUpdater] = None


def get_updater() -> Optional[AutoUpdater]:
    """Get the global AutoUpdater instance."""
    return _updater


def init_updater(current_version: str, app_slug: str = "backup-verification"):
    """Initialize the global AutoUpdater instance."""
    global _updater
    _updater = AutoUpdater(current_version, app_slug)
    return _updater
