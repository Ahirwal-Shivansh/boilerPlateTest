"""
macOS PKG build pipeline for EG-Agent, usable from any project that
has `eg-agent` installed.

This version is packaged inside `eg_agent` so that **desktop applications**
can depend on it via:

    pip install eg-agent

and then build a PKG from their own project root via:

    eg-agent-build macos

Key behavior:
- Uses the **current working directory** as the project root.
- Builds two PyInstaller binaries into `./dist`:
  - the main app/agent binary
  - the installer helper binary
- Creates a payload layout under `./build/macos/payload`.
- Generates a `postinstall` script that calls the helper.
- Invokes `pkgbuild` to produce:
  `./build/macos/EG-Agent.pkg`.

By default the app entrypoint is `eg_agent/templates/eg_agent_standalone.py`
from the installed package, but this can be overridden per-project via the
`EG_AGENT_BUILD_ENTRY` environment variable.
"""

from __future__ import annotations

import os
import subprocess
import sys
from importlib import resources, import_module
from pathlib import Path
from textwrap import dedent
from typing import Optional, Tuple


PROJECT_ROOT = Path.cwd()
BUILD_ROOT = PROJECT_ROOT / "build" / "macos"
DIST_ROOT = PROJECT_ROOT / "dist"

# Load .env file if it exists
_env_file = PROJECT_ROOT / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file, override=False)
    except ImportError:
        pass  # dotenv not installed, skip


def _run(cmd: list[str]) -> None:
    print(f"[eg-agent-build-macos] $ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def _get_tray_icon_path_from_env() -> Optional[Path]:
    """
    Read .env and return the path for EG_AGENT_TRAY_ICON if set and the file exists.
    Path is resolved relative to PROJECT_ROOT. Returns None if not set or file missing.
    """
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return None
    value = None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("EG_AGENT_TRAY_ICON="):
            value = line.split("=", 1)[1].strip().strip("'\"")
            break
    if not value:
        return None
    p = Path(value)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve() if p.exists() else None


def _resolve_agent_entry() -> Path:
    """
    Determine which Python script PyInstaller should freeze for the *app*.

    Precedence:
    1) EG_AGENT_BUILD_ENTRY env var (path relative to CWD or absolute).
    2) Default: the packaged standalone script:
       `eg_agent/templates/eg_agent_standalone.py`
    """

    override = os.environ.get("EG_AGENT_BUILD_ENTRY")
    if override:
        p = Path(override)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p.resolve()

    tmpl = resources.files("eg_agent").joinpath("templates/eg_agent_standalone.py")
    return Path(tmpl)


def _resolve_helper_entry() -> Path:
    """
    Determine the script for the installer helper.

    We always freeze the installed `installer_helper.main` module.
    """

    mod = import_module("installer_helper.main")
    return Path(mod.__file__).resolve()


def _resolve_worker_entry() -> Path:
    """
    Determine the script for the standalone worker entry.

    We freeze `eg_agent.worker:main` into its own binary so that the packaged
    agent can spawn workers even when running as a frozen binary.
    """

    mod = import_module("eg_agent.worker")
    return Path(mod.__file__).resolve()


def build_pyinstaller_binaries(
    *,
    app_name: Optional[str] = None,
) -> Tuple[Path, Path, Path, str]:
    """
    Build macOS agent + installer helper + worker binaries via PyInstaller.

    Executable names are derived from app_name:
    - agent: <app_name>
    - helper: <app_name>-installer-helper
    - worker: <app_name>-worker

    Returns: (agent_bin, helper_bin, worker_bin, app_name)
    """
    if app_name is None:
        app_name = os.environ.get("EG_AGENT_APP_NAME", "eg-agent")

    agent_name = app_name
    helper_name = f"{app_name}-installer-helper"
    worker_name = f"{app_name}-worker"

    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    DIST_ROOT.mkdir(parents=True, exist_ok=True)

    agent_entry = _resolve_agent_entry()
    helper_entry = _resolve_helper_entry()
    worker_entry = _resolve_worker_entry()

    # Bundle tasks.py and related files from project root
    tasks_file = PROJECT_ROOT / "tasks.py"
    # Example:
    # verification_db_file = PROJECT_ROOT / "verification_db.py"
    # constants_file = PROJECT_ROOT / "constants.py"
    env_file = PROJECT_ROOT / ".env"

    # 1) Agent binary - bundle tasks.py and related files
    agent_cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--onefile",
        "--name",
        agent_name,
        "--distpath",
        str(DIST_ROOT),
        "--workpath",
        str(BUILD_ROOT / "pyinstaller-work"),
        "--specpath",
        str(BUILD_ROOT),
    ]

    # Bundle tasks.py and related files from project root
    if tasks_file.exists():
        agent_cmd.extend(["--add-data", f"{tasks_file}:."])
    # Example:
    # if verification_db_file.exists():
    #     agent_cmd.extend(["--add-data", f"{verification_db_file}:."])
    # if constants_file.exists():
    #     agent_cmd.extend(["--add-data", f"{constants_file}:."])
    if env_file.exists():
        agent_cmd.extend(["--add-data", f"{env_file}:."])
    # Bundle tray icon if EG_AGENT_TRAY_ICON is set in .env (for menu bar icon)
    tray_icon_path = _get_tray_icon_path_from_env()
    if tray_icon_path is not None:
        agent_cmd.extend(["--add-data", f"{tray_icon_path}:."])

    agent_cmd.append(str(agent_entry))
    _run(agent_cmd)

    # 2) Helper binary
    helper_cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--onefile",
        "--name",
        helper_name,
        "--distpath",
        str(DIST_ROOT),
        "--workpath",
        str(BUILD_ROOT / "pyinstaller-work"),
        "--specpath",
        str(BUILD_ROOT),
    ]

    # Bundle installer_hooks.py if it exists in the project
    hooks_file = PROJECT_ROOT / "installer_hooks.py"
    if hooks_file.exists():
        helper_cmd.extend([
            "--add-data",
            f"{hooks_file}:.",
            "--hidden-import",
            "installer_hooks",
        ])

    helper_cmd.append(str(helper_entry))
    _run(helper_cmd)

    # 3) Worker binary - also bundle tasks.py and related files
    worker_cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--onefile",
        "--name",
        worker_name,
        "--distpath",
        str(DIST_ROOT),
        "--workpath",
        str(BUILD_ROOT / "pyinstaller-work"),
        "--specpath",
        str(BUILD_ROOT),
    ]

    # Bundle tasks.py and related files for workers too
    if tasks_file.exists():
        worker_cmd.extend(["--add-data", f"{tasks_file}:."])
    # Example:
    # if verification_db_file.exists():
    #     worker_cmd.extend(["--add-data", f"{verification_db_file}:."])
    # if constants_file.exists():
    #     worker_cmd.extend(["--add-data", f"{constants_file}:."])
    if env_file.exists():
        worker_cmd.extend(["--add-data", f"{env_file}:."])

    worker_cmd.append(str(worker_entry))
    _run(worker_cmd)

    return (
        DIST_ROOT / agent_name,
        DIST_ROOT / helper_name,
        DIST_ROOT / worker_name,
        app_name,
    )


def create_app_bundle(
    *,
    executable_path: Path,
    app_name: str,
    output_dir: Path,
    executable_name: str,
    app_dir: Path,
) -> Path:
    """
    Create a macOS .app bundle wrapper for the executable.

    This creates an .app bundle that can be used as a desktop shortcut
    and will appear in Finder/Launchpad. The .app bundle references
    the executable in the app directory.
    """
    app_bundle = output_dir / f"{app_name}.app"
    contents_dir = app_bundle / "Contents"
    macos_dir = contents_dir / "MacOS"
    resources_dir = contents_dir / "Resources"

    # Create directory structure
    # Ensure parent directories have proper permissions
    import stat
    macos_dir.mkdir(parents=True, exist_ok=True)
    resources_dir.mkdir(parents=True, exist_ok=True)

    # Try to set permissions, but don't fail if we can't
    # (directories might already have correct permissions or be writable anyway)
    for dir_path in [macos_dir, resources_dir]:
        try:
            dir_path.chmod(0o755)
        except (PermissionError, OSError):
            # Permissions might be set by system or we might not have permission to change them
            # This is okay - we'll try to write files anyway
            pass

    # Create a wrapper script that launches the actual executable
    wrapper_script = macos_dir / app_name

    # Remove existing file if it exists (might have wrong permissions)
    if wrapper_script.exists():
        import subprocess
        import os
        script_path = str(wrapper_script)

        # Aggressively try to remove macOS-specific flags and attributes
        try:
            subprocess.run(
                ["xattr", "-c", script_path],
                capture_output=True, check=False, timeout=2)
        except Exception:
            pass

        try:
            subprocess.run(
                ["chflags", "nouchg", script_path],
                capture_output=True, check=False, timeout=2)
        except Exception:
            pass

        # Try multiple removal methods
        removed = False
        try:
            wrapper_script.unlink()
            removed = True
        except (PermissionError, OSError):
            try:
                # Try using os.remove directly
                os.remove(script_path)
                removed = True
            except (PermissionError, OSError):
                try:
                    # Try to fix permissions first
                    os.chmod(script_path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
                    os.remove(script_path)
                    removed = True
                except Exception:
                    pass

        if not removed:
            print(f"[eg-agent-build-macos] Warning: Could not remove existing wrapper script at {script_path}")
            print(f"[eg-agent-build-macos] Attempting to overwrite using direct file operations...")

    # Write the wrapper script - try multiple methods
    script_content = dedent(f"""#!/bin/bash
# Wrapper script to launch {app_name}
exec "{app_dir}/{executable_name}" "$@"
""")

    import tempfile
    import shutil

    written = False
    try:
        # Try write_text first (normal case)
        wrapper_script.write_text(script_content, encoding="utf-8")
        written = True
    except (PermissionError, OSError):
        # If that fails, try writing to a temp file and moving it
        try:
            # Write to a temp file in the same directory
            temp_file = macos_dir / f".{app_name}.tmp"
            with open(temp_file, 'w', encoding='utf-8') as f:
                f.write(script_content)
            # Try to move it (this might work even if direct write doesn't)
            if wrapper_script.exists():
                # Remove the old file first if possible
                try:
                    wrapper_script.unlink()
                except Exception:
                    pass
            temp_file.replace(wrapper_script)
            written = True
        except Exception as e1:
            # Last resort: try using open() with truncation
            try:
                import os
                # Use os.open with O_TRUNC to force truncation
                fd = os.open(str(wrapper_script), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o755)
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    f.write(script_content)
                written = True
            except Exception as e2:
                # If all methods fail, raise an error
                print(f"[eg-agent-build-macos] Error: Could not write wrapper script after multiple attempts")
                print(f"[eg-agent-build-macos] Attempt 1 (write_text): Permission denied")
                print(f"[eg-agent-build-macos] Attempt 2 (temp file + move): {e1}")
                print(f"[eg-agent-build-macos] Attempt 3 (os.open with O_TRUNC): {e2}")
                raise RuntimeError(f"Could not create app bundle wrapper script due to permission issues. "
                                 f"Please manually remove: {wrapper_script} and try again, or run: "
                                 f"sudo rm -rf '{macos_dir.parent.parent}'")

    if not written:
        raise RuntimeError("Failed to write wrapper script")

    # Try to set executable permissions, but don't fail if we can't
    try:
        wrapper_script.chmod(0o755)
    except (PermissionError, OSError):
        # File might already be executable or we might not have permission to change it
        # This is okay - the file should still work
        pass

    # Create Info.plist
    info_plist = contents_dir / "Info.plist"
    info_plist.write_text(dedent(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>{app_name}</string>
    <key>CFBundleIdentifier</key>
    <string>com.eg.agent</string>
    <key>CFBundleName</key>
    <string>{app_name}</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.13</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
"""), encoding="utf-8")

    return app_bundle


def create_payload_layout(
    *,
    agent_bin: Path,
    helper_bin: Path,
    worker_bin: Path,
    app_dir: Path,
    app_name: str,
    create_desktop_shortcut: bool = True,
) -> Path:
    """
    Create the directory structure that will be installed by the PKG.

    Installs into `/Applications/<app_name>/` with executables:
    - <app_name>
    - <app_name>-installer-helper
    - <app_name>-worker
    """

    payload_root = BUILD_ROOT / "payload"
    install_root = payload_root / app_dir.relative_to(Path("/"))

    install_root.mkdir(parents=True, exist_ok=True)

    target_agent = install_root / agent_bin.name
    target_helper = install_root / helper_bin.name
    target_worker = install_root / worker_bin.name

    target_agent.write_bytes(agent_bin.read_bytes())
    target_helper.write_bytes(helper_bin.read_bytes())
    target_worker.write_bytes(worker_bin.read_bytes())

    for p in (target_agent, target_helper, target_worker):
        try:
            mode = p.stat().st_mode
            p.chmod(mode | 0o111)
        except (PermissionError, OSError):
            # File might already be executable or we might not have permission to change it
            # This is okay - continue anyway
            pass

    # Create .app bundle for desktop shortcut (installed in /Applications/)
    if create_desktop_shortcut:
        applications_dir = payload_root / "Applications"
        applications_dir.mkdir(parents=True, exist_ok=True)
        # Use app_name for the .app bundle name (capitalize first letter of each word)
        # e.g., "backup-verification" -> "Backup-Verification"
        app_bundle_name = app_name.replace("-", " ").title().replace(" ", "-")

        # Remove existing app bundle if it exists (from previous build)
        existing_bundle = applications_dir / f"{app_bundle_name}.app"
        bundle_removed = False
        if existing_bundle.exists():
            import shutil
            import subprocess
            try:
                # First, try to remove macOS-specific flags and attributes
                bundle_path = str(existing_bundle)
                try:
                    # Remove extended attributes (quarantine, etc.)
                    subprocess.run(
                        ["xattr", "-rc", bundle_path],
                        capture_output=True, check=False, timeout=5)
                except Exception:
                    pass  # xattr might not be available or might fail

                try:
                    # Remove user immutable flag if set
                    subprocess.run(
                        ["chflags", "-R", "nouchg", bundle_path],
                        capture_output=True, check=False, timeout=5)
                except Exception:
                    pass  # chflags might fail

                # Now try to remove the directory
                shutil.rmtree(existing_bundle)
                bundle_removed = True
            except (PermissionError, OSError) as e:
                # If we still can't remove it, try one more time with more aggressive permissions
                try:
                    import os
                    import stat
                    # Fix permissions recursively
                    for root, dirs, files in os.walk(existing_bundle):
                        for d in dirs:
                            try:
                                os.chmod(os.path.join(root, d), 
                                        stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
                            except Exception:
                                pass
                        for f in files:
                            try:
                                os.chmod(os.path.join(root, f), 
                                        stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)
                            except Exception:
                                pass
                    shutil.rmtree(existing_bundle)
                    bundle_removed = True
                except Exception as e2:
                    # If we still can't remove it, log a warning and continue
                    # The create_app_bundle function will try to overwrite individual files
                    print(f"[eg-agent-build-macos] Warning: Could not fully remove existing app bundle: {e2}")
                    print(f"[eg-agent-build-macos] Will attempt to overwrite individual files - build may succeed")

        # If bundle still exists and we couldn't remove it, try to remove key files individually
        if existing_bundle.exists() and not bundle_removed:
            try:
                import subprocess
                wrapper_path = existing_bundle / "Contents" / "MacOS" / app_bundle_name
                if wrapper_path.exists():
                    try:
                        subprocess.run(["xattr", "-c", str(wrapper_path)], 
                                     capture_output=True, check=False, timeout=2)
                        subprocess.run(["chflags", "nouchg", str(wrapper_path)], 
                                     capture_output=True, check=False, timeout=2)
                    except Exception:
                        pass
            except Exception:
                pass  # Best effort - continue anyway

        try:
            app_bundle = create_app_bundle(
                executable_path=target_agent,
                app_name=app_bundle_name,
                output_dir=applications_dir,  # Install .app in /Applications/
                executable_name=agent_bin.name,
                app_dir=app_dir,
            )
            print(f"[eg-agent-build-macos] Created .app bundle: {app_bundle}")
        except Exception as e:
            print(f"[eg-agent-build-macos] Warning: Could not create .app bundle: {e}")
            print(f"[eg-agent-build-macos] Build will continue without desktop shortcut - you can create it manually later")
            # Continue without the app bundle - it's optional

    return payload_root


def create_postinstall_script(
    *,
    app_dir: Path,
    agent_name: str,
    helper_name: str,
    app_name: str,
    launch_agent_plist: Optional[str] = None,
) -> Path:
    """
    Create a `postinstall` script that runs after files are installed.

    It is intentionally minimal and delegates all heavy logic to the
    Python helper.
    """

    scripts_dir = BUILD_ROOT / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    postinstall_path = scripts_dir / "postinstall"

    app_dir_abs = str(app_dir)
    agent_cli = os.path.join(app_dir_abs, agent_name)
    helper_bin = os.path.join(app_dir_abs, helper_name)

    script = "#!/bin/bash\n" + dedent(
        f"""
        set -euo pipefail

        echo "[postinstall] Starting {app_name} postinstall script"

        # Postinstall runs as root. To avoid permission issues on first run
        # (e.g. root-owned log/db files), we should prefer running the helper
        # and restarting the app as the logged-in user (when available).
        LOGGED_IN_USER=""
        USER_HOME=""
        LOGGED_IN_UID=""
        LOGGED_IN_GID=""

        LAUNCH_PLIST="{launch_agent_plist or ''}"
        if [[ -n "$LAUNCH_PLIST" ]]; then
          echo "[postinstall] Unloading LaunchAgent: $LAUNCH_PLIST"
          launchctl unload "$LAUNCH_PLIST" || true
        fi

        # Best-effort logged-in user detection.
        # /dev/console usually reflects the active login session.
        LOGGED_IN_USER=$(stat -f "%Su" /dev/console 2>/dev/null || echo "")
        if [[ -z "$LOGGED_IN_USER" ]]; then
          LOGGED_IN_USER=$(who | awk '{{print $1}}' | head -1 || echo "")
        fi

        if [[ -n "$LOGGED_IN_USER" && "$LOGGED_IN_USER" != "root" ]]; then
          USER_HOME=$(eval echo ~"$LOGGED_IN_USER" 2>/dev/null || echo "")
          LOGGED_IN_UID=$(id -u "$LOGGED_IN_USER" 2>/dev/null || echo "")
          LOGGED_IN_GID=$(id -g "$LOGGED_IN_USER" 2>/dev/null || echo "")
        fi

        # Ensure user-writable log/data directories exist before running
        # the installer helper (which may initialize DB and write logs).
        if [[ -n "$USER_HOME" && -n "$LOGGED_IN_UID" && "$LOGGED_IN_UID" != "0" ]]; then
          for d in \
            "$USER_HOME/Library/Application Support/eg-agent" \
            "$USER_HOME/Library/Application Support/EG-Agent" ; do
            mkdir -p "$d"
            # Use numeric ownership to avoid reliance on group names existing.
            chown -R "$LOGGED_IN_UID:$LOGGED_IN_GID" "$d" >/dev/null 2>&1 || true
            chmod -R u+rwX "$d" >/dev/null 2>&1 || true
          done

          # Remove any root-owned log file from a previous install attempt.
          rm -f \
            "$USER_HOME/Library/Application Support/eg-agent/eg_agent.log" \
            "$USER_HOME/Library/Application Support/EG-Agent/eg_agent.log" \
            >/dev/null 2>&1 || true
        else
          echo "[postinstall] Warning: Could not determine logged-in user (skipping pre-create/chown)."
        fi

        echo "[postinstall] Running installer helper"
        set +e
        HELPER_RC=0
        if [[ -n "$LOGGED_IN_UID" && "$LOGGED_IN_UID" != "0" ]]; then
          echo "[postinstall] Running installer helper as UID: $LOGGED_IN_UID"
          launchctl asuser "$LOGGED_IN_UID" "{helper_bin}" --full-install --agent-cli "{agent_cli}" --is-upgrade
          HELPER_RC=$?
          if [[ "$HELPER_RC" != "0" ]]; then
            echo "[postinstall] Warning: launchctl asuser failed (rc=$HELPER_RC); running installer helper as root"
            "{helper_bin}" --full-install --agent-cli "{agent_cli}" --is-upgrade
            HELPER_RC=$?
          fi
        else
          echo "[postinstall] Warning: Could not determine user UID; running installer helper as root may cause permission issues"
          "{helper_bin}" --full-install --agent-cli "{agent_cli}" --is-upgrade
          HELPER_RC=$?
        fi
        set -e

        if [[ "$HELPER_RC" != "0" ]]; then
          echo "[postinstall] Warning: installer helper failed (rc=$HELPER_RC). Install will continue; first app run may need to initialize DB/logs."
        fi

        # If the helper had to run as root (fallback), ensure created files
        # become user-owned so the app doesn't crash on first run.
        if [[ -n "$USER_HOME" && -n "$LOGGED_IN_UID" && "$LOGGED_IN_UID" != "0" ]]; then
          for d in \
            "$USER_HOME/Library/Application Support/eg-agent" \
            "$USER_HOME/Library/Application Support/EG-Agent" ; do
            chown -R "$LOGGED_IN_UID:$LOGGED_IN_GID" "$d" >/dev/null 2>&1 || true
          done
        fi

        if [[ -n "$LAUNCH_PLIST" ]]; then
          echo "[postinstall] Loading LaunchAgent: $LAUNCH_PLIST"
          launchctl load "$LAUNCH_PLIST" || true
        fi

        # Create desktop shortcut (alias) for the application
        # Note: This is optional and failures should not block installation
        echo "[postinstall] Creating desktop shortcut..."
        DESKTOP_SHORTCUT_ENABLED="${{EG_AGENT_CREATE_DESKTOP_SHORTCUT:-true}}"
        if [[ "$DESKTOP_SHORTCUT_ENABLED" == "true" ]]; then
          # Temporarily disable strict error handling for this optional feature
          set +e
          set +u

          if [[ -n "$LOGGED_IN_USER" && "$LOGGED_IN_USER" != "root" ]]; then
            if [[ -n "$USER_HOME" ]]; then
              DESKTOP_DIR="$USER_HOME/Desktop"
              # Convert app_name to title case for .app bundle name (e.g., "backup-verification" -> "Backup-Verification")
              APP_BUNDLE_NAME=$(echo "{app_name}" | sed 's/-/ /g' | awk '{{for(i=1;i<=NF;i++){{$i=toupper(substr($i,1,1)) tolower(substr($i,2))}}}}1}}' | sed 's/ /-/g' || echo "{app_name}")
              APP_BUNDLE="/Applications/$APP_BUNDLE_NAME.app"

              if [[ -d "$APP_BUNDLE" && -d "$DESKTOP_DIR" ]]; then
                LOGGED_IN_UID=$(id -u "$LOGGED_IN_USER" 2>/dev/null || echo "")
                if [[ -n "$LOGGED_IN_UID" && "$LOGGED_IN_UID" != "0" ]]; then
                  # Try to create desktop shortcut using osascript
                  # Note: Using unquoted heredoc so bash variables are expanded
                  SHORTCUT_RESULT=$(launchctl asuser "$LOGGED_IN_UID" osascript <<EOFSCRIPT 2>&1 || echo "error: osascript failed"
tell application "Finder"
  try
    make alias file to POSIX file "$APP_BUNDLE" at POSIX file "$DESKTOP_DIR"
    return "success"
  on error errMsg
    return "error: " & errMsg
  end try
end tell
EOFSCRIPT
)
                  if echo "$SHORTCUT_RESULT" | grep -q "success" 2>/dev/null; then
                    echo "[postinstall] Desktop shortcut created successfully for user: $LOGGED_IN_USER"
                  else
                    echo "[postinstall] Warning: Could not create desktop shortcut (this is non-critical)"
                    echo "[postinstall] Installation will continue successfully"
                  fi
                else
                  echo "[postinstall] Warning: Could not determine user UID for desktop shortcut"
                fi
              else
                echo "[postinstall] Info: Skipping desktop shortcut (app bundle or Desktop directory not accessible)"
              fi
            else
              echo "[postinstall] Warning: Could not determine user home directory"
            fi
          else
            echo "[postinstall] Info: Skipping desktop shortcut (no logged-in user detected)"
          fi

          # Re-enable strict error handling
          set -e
          set -u
        fi

        # Restart the application after update
        # Note: postinstall runs as root, but we want to launch app as the logged-in user
        echo "[postinstall] Restarting application..."
        if [[ -f "{agent_cli}" ]]; then
          # Wait a moment for installer to finish and files to be written
          sleep 2

          # LOGGED_IN_UID is computed earlier in this script.

          if [[ -n "$LOGGED_IN_UID" && "$LOGGED_IN_UID" != "0" ]]; then
            # Launch as the logged-in user using launchctl asuser (no password needed)
            echo "[postinstall] Launching application as UID: $LOGGED_IN_UID"
            launchctl asuser "$LOGGED_IN_UID" "{agent_cli}" > /dev/null 2>&1 &
          else
            # Do not launch as root: it can recreate root-owned log/db files.
            echo "[postinstall] Warning: Could not determine user UID; skipping app restart to avoid permission issues"
          fi
          echo "[postinstall] Application restart initiated"
        else
          echo "[postinstall] Warning: Application binary not found at {agent_cli}"
        fi

        echo "[postinstall] Completed {app_name} postinstall script"
        exit 0
        """
    )

    postinstall_path.write_text(script, encoding="utf-8")
    postinstall_path.chmod(postinstall_path.stat().st_mode | 0o111)
    return scripts_dir


def build_pkg(
    *,
    payload_root: Path,
    scripts_dir: Path,
    identifier: str = "com.eg.agent",
    version: Optional[str] = None,
    pkg_output: Optional[Path] = None,
    pkgbuild_path: str = "pkgbuild",
) -> Path:
    """
    Build a component PKG using `pkgbuild`.

    Version precedence:
    1) EG_AGENT_BUILD_VERSION env var
    2) version parameter
    3) Default: "1.0.0"

    Package name precedence:
    1) EG_AGENT_PKG_NAME env var
    2) pkg_output parameter
    3) Default: "EG-Agent.pkg"
    """
    if version is None:
        version = os.environ.get("EG_AGENT_BUILD_VERSION", "1.0.0")

    if pkg_output is None:
        pkg_name = os.environ.get("EG_AGENT_PKG_NAME", "EG-Agent.pkg")
        pkg_output = BUILD_ROOT / pkg_name

    _run(
        [
            pkgbuild_path,
            "--root",
            str(payload_root),
            "--scripts",
            str(scripts_dir),
            "--identifier",
            identifier,
            "--version",
            version,
            str(pkg_output),
        ]
    )

    return pkg_output


def main() -> None:
    """
    Build the macOS PKG end‑to‑end for the current project.

    Intended to be called by the `eg-agent-build macos` console script.
    """

    # Get app name from environment
    app_name = os.environ.get("EG_AGENT_APP_NAME", "eg-agent")

    # Build binaries with app name
    agent_bin, helper_bin, worker_bin, app_name = build_pyinstaller_binaries(app_name=app_name)

    # Create app directory path: /Applications/<app-name>/
    app_dir = Path(f"/Applications/{app_name}")

    # Check if desktop shortcut should be created
    create_shortcut = os.environ.get("EG_AGENT_CREATE_DESKTOP_SHORTCUT", "true").lower() == "true"

    payload_root = create_payload_layout(
        agent_bin=agent_bin,
        helper_bin=helper_bin,
        worker_bin=worker_bin,
        app_dir=app_dir,
        app_name=app_name,
        create_desktop_shortcut=create_shortcut,
    )
    scripts_dir = create_postinstall_script(
        app_dir=app_dir,
        agent_name=agent_bin.name,
        helper_name=helper_bin.name,
        app_name=app_name,
        launch_agent_plist=None,
    )
    pkg_path = build_pkg(payload_root=payload_root, scripts_dir=scripts_dir)
    print(f"[eg-agent-build-macos] Built PKG at: {pkg_path}")


if __name__ == "__main__":  # pragma: no cover - CLI entry
    main()
