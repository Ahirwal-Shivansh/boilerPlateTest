"""
Windows MSI build pipeline for EG-Agent, usable from any project that
has `eg-agent` installed.

This version is packaged inside `eg_agent` so that **desktop applications**
can depend on it via:

    pip install eg-agent

and then build an MSI from their own project root via:

    eg-agent-build windows

Key behavior:
- Uses the **current working directory** as the project root.
- Builds two PyInstaller binaries into `./dist`:
  - the main app/agent binary
  - the installer helper binary
- Writes WiX files into `./build/windows/wix`.
- Invokes `candle.exe` + `light.exe` (must be on PATH) to produce:
  `./build/windows/EG-Agent.msi`.

By default the app entrypoint is `eg_agent/templates/eg_agent_standalone.py`
from the installed package, but this can be overridden per-project via the
`EG_AGENT_BUILD_ENTRY` environment variable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib import resources, import_module
from pathlib import Path
from textwrap import dedent
from typing import Optional, Tuple


PROJECT_ROOT = Path.cwd()
BUILD_ROOT = PROJECT_ROOT / "build" / "windows"
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
    print(f"[eg-agent-build-windows] $ {' '.join(cmd)}")
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

    # Fall back to the packaged template script.
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

    We freeze `eg_agent.worker:main` into its own EXE so that the packaged
    agent can spawn workers even when running as a frozen binary.
    """

    mod = import_module("eg_agent.worker")
    return Path(mod.__file__).resolve()


def build_pyinstaller_binaries(
    *,
    app_name: Optional[str] = None,
    helper_name: Optional[str] = None,
    worker_name: Optional[str] = None,
) -> Tuple[Path, Path, Path]:
    """
    Build the agent EXE and installer helper EXE with PyInstaller.

    The resulting binaries are placed under `./dist`.
    """

    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    DIST_ROOT.mkdir(parents=True, exist_ok=True)

    if app_name is None:
        app_name = os.environ.get("EG_AGENT_APP_NAME", "eg-agent")
    
    app_name = app_name
    helper_name = f"{app_name}-installer-helper"
    worker_name = f"{app_name}-worker"

    agent_entry = _resolve_agent_entry()
    helper_entry = _resolve_helper_entry()
    worker_entry = _resolve_worker_entry()

    # 1) Agent binary - bundle tasks.py and related files from project root
    agent_cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--onefile",
        "--name",
        app_name,
        "--distpath",
        str(DIST_ROOT),
        "--workpath",
        str(BUILD_ROOT / "pyinstaller-work"),
        "--specpath",
        str(BUILD_ROOT),
    ]
    
    # Bundle tasks.py and related files from project root
    # Windows uses semicolon separator for --add-data
    tasks_file = PROJECT_ROOT / "tasks.py"
    verification_db_file = PROJECT_ROOT / "verification_db.py"
    constants_file = PROJECT_ROOT / "constants.py"
    env_file = PROJECT_ROOT / ".env"
    
    if tasks_file.exists():
        agent_cmd.extend(["--add-data", f"{tasks_file};."])
    if verification_db_file.exists():
        agent_cmd.extend(["--add-data", f"{verification_db_file};."])
    if constants_file.exists():
        agent_cmd.extend(["--add-data", f"{constants_file};."])
    if env_file.exists():
        agent_cmd.extend(["--add-data", f"{env_file};."])
    # Bundle tray icon if EG_AGENT_TRAY_ICON is set in .env (for system tray icon)
    tray_icon_path = _get_tray_icon_path_from_env()
    if tray_icon_path is not None:
        agent_cmd.extend(["--add-data", f"{tray_icon_path};."])
    agent_cmd.extend([
        "--hidden-import", "tkinter",
        "--hidden-import", "_tkinter",
        "--hidden-import", "tkinter.filedialog",
        "--hidden-import", "tkinter.constants",
        "--hidden-import", "tkinter.dialog",
        "--hidden-import", "tkinter.commondialog",
    ])
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
            f"{hooks_file};.",
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
    # Windows uses semicolon separator for --add-data
    if tasks_file.exists():
        worker_cmd.extend(["--add-data", f"{tasks_file};."])
    if verification_db_file.exists():
        worker_cmd.extend(["--add-data", f"{verification_db_file};."])
    if constants_file.exists():
        worker_cmd.extend(["--add-data", f"{constants_file};."])
    if env_file.exists():
        worker_cmd.extend(["--add-data", f"{env_file};."])
    
    worker_cmd.append(str(worker_entry))
    _run(worker_cmd)

    return (
        DIST_ROOT / f"{app_name}.exe",
        DIST_ROOT / f"{helper_name}.exe",
        DIST_ROOT / f"{worker_name}.exe",
    )


def generate_wix_wxs(
    *,
    agent_exe: Path,
    helper_exe: Path,
    worker_exe: Path,
    workers_config: Path,
    product_version: Optional[str] = None,
    manufacturer: str = "EG",
    upgrade_code: str = "{11111111-2222-3333-4444-555555555555}",
    app_name: Optional[str] = None,
    create_desktop_shortcut: bool = True,
) -> Path:
    """
    Generate a WiX .wxs file for the agent.

    Version precedence:
    1) EG_AGENT_BUILD_VERSION env var
    2) product_version parameter
    3) Default: "1.0.0"

    - `upgrade_code` must be **stable across versions** of the product.
      Replace it with a GUID generated once for the application using
      this build pipeline.
    - We install under `ProgramFilesFolder` as a per-machine install, which
      avoids ICE64/ICE91 issues around per-user profile directories.
    """
    if product_version is None:
        product_version = os.environ.get("EG_AGENT_BUILD_VERSION", "1.0.0")
    if app_name is None:
        app_name = os.environ.get("EG_AGENT_APP_NAME", "EG-Agent")

    # Title-case the app name for display (e.g. "backup-verification" -> "Backup Verification")
    display_name = app_name.replace("-", " ").title()

    wix_dir = BUILD_ROOT / "wix"
    wix_dir.mkdir(parents=True, exist_ok=True)

    agent_filename = agent_exe.name
    helper_filename = helper_exe.name
    worker_filename = worker_exe.name
    workers_config_filename = workers_config.name

    wxs_path = wix_dir / "eg_agent.wxs"

    # Desktop shortcut component (only included if requested)
    shortcut_component = ""
    shortcut_feature_ref = ""
    if create_desktop_shortcut:
        shortcut_component = f"""
                    <Component Id="cmpDesktopShortcut" Guid="{{E7A3B5D1-4C2F-4E8A-B1D6-9F0C3A7E5B42}}">
                      <Shortcut Id="DesktopShortcut"
                          Name="{display_name}"
                          Description="{display_name}"
                          Target="[#filAgentExe]"
                          WorkingDirectory="INSTALLDIR"
                          Directory="DesktopFolder" />
                      <RegistryValue
                          Root="HKCU"
                          Key="Software\\EG\\EG-Agent"
                          Name="DesktopShortcut"
                          Type="integer"
                          Value="1"
                          KeyPath="yes" />
                    </Component>"""
        shortcut_feature_ref = """
              <ComponentRef Id="cmpDesktopShortcut" />"""

    wxs_content = dedent(
        f"""
        <?xml version="1.0" encoding="UTF-8"?>
        <Wix xmlns="http://schemas.microsoft.com/wix/2006/wi">
          <Product
              Id="*"
              Name="{display_name}"
              Language="1033"
              Version="{product_version}"
              Manufacturer="{manufacturer}"
              UpgradeCode="{upgrade_code}">

            <Package
                InstallerVersion="500"
                Compressed="yes"
                InstallScope="perMachine" />

            <MajorUpgrade
                DowngradeErrorMessage="A newer version of {display_name} is already installed." />

            <MediaTemplate EmbedCab="yes" />

            <Feature Id="MainFeature" Title="{display_name}" Level="1">
              <ComponentRef Id="cmpAgentExe" />
              <ComponentRef Id="cmpHelperExe" />
              <ComponentRef Id="cmpWorkerExe" />
              <ComponentRef Id="cmpWorkersConfig" />{shortcut_feature_ref}
            </Feature>

            <Directory Id="TARGETDIR" Name="SourceDir">
              <Directory Id="ProgramFilesFolder">
                <Directory Id="ManufacturerDir" Name="{manufacturer}">
                  <Directory Id="INSTALLDIR" Name="{display_name}">
                    <Component Id="cmpAgentExe" Guid="{{C89B8C2C-5B0A-4F3D-9A3E-24F6D7A9F101}}">
                      <File Id="filAgentExe" Source="{agent_exe}" Name="{agent_filename}" />
                      <RegistryValue
                          Id="regAgentExe"
                          Root="HKLM"
                          Key="Software\\EG\\EG-Agent"
                          Name="AgentExeInstalled"
                          Type="integer"
                          Value="1"
                          KeyPath="yes" />
                    </Component>
                    <Component Id="cmpHelperExe" Guid="{{9F5D7A43-6E07-4F4E-9D0D-8C7F9837A4B2}}">
                      <File Id="filHelperExe" Source="{helper_exe}" Name="{helper_filename}" />
                      <RegistryValue
                          Id="regHelperExe"
                          Root="HKLM"
                          Key="Software\\EG\\EG-Agent"
                          Name="HelperExeInstalled"
                          Type="integer"
                          Value="1"
                          KeyPath="yes" />
                    </Component>
                    <Component Id="cmpWorkerExe" Guid="{{AA5E5D0E-660C-4A2A-9B9D-6B2F8F8C3E10}}">
                      <File Id="filWorkerExe" Source="{worker_exe}" Name="{worker_filename}" />
                      <RegistryValue
                          Id="regWorkerExe"
                          Root="HKLM"
                          Key="Software\\EG\\EG-Agent"
                          Name="WorkerExeInstalled"
                          Type="integer"
                          Value="1"
                          KeyPath="yes" />
                    </Component>
                    <Component Id="cmpWorkersConfig" Guid="{{3B9E8C1F-2D4C-4F6E-A6D3-7C8F9B0A1E23}}">
                      <File Id="filWorkersJson" Source="{workers_config}" Name="{workers_config_filename}" KeyPath="yes" />
                    </Component>{shortcut_component}
                  </Directory>
                </Directory>
              </Directory>
              <Directory Id="DesktopFolder" Name="Desktop" />
            </Directory>

            <CustomAction
                Id="RunInstallerHelper"
                FileKey="filHelperExe"
                ExeCommand="--full-install --agent-cli &quot;[#filAgentExe]&quot; --is-upgrade"
                Execute="deferred"
                Return="check"
                Impersonate="no" />

            <InstallExecuteSequence>
              <Custom Action="RunInstallerHelper" After="InstallFiles">NOT REMOVE</Custom>
            </InstallExecuteSequence>
          </Product>
        </Wix>
        """
    ).strip()

    wxs_path.write_text(wxs_content, encoding="utf-8")
    return wxs_path


def build_msi(
    *,
    wxs_path: Path,
    msi_output: Optional[Path] = None,
    candle_path: str = "candle",
    light_path: str = "light",
) -> Path:
    """
    Run candle.exe + light.exe to compile WiX source to an MSI.

    - WiX must be installed and `candle` / `light` must be on PATH.
    """
    app_name = os.environ.get("EG_AGENT_APP_NAME", "eg-agent")
    if msi_output is None:
        msi_output = BUILD_ROOT / f"{app_name}.msi"

    wix_obj = wxs_path.with_suffix(".wixobj")

    _run([candle_path, "-o", str(wix_obj), str(wxs_path)])
    _run(
        [
            light_path,
            "-o",
            str(msi_output),
            str(wix_obj),
        ]
    )

    return msi_output


def main() -> None:
    """
    Build the Windows MSI end‑to‑end for the current project.

    Intended to be called by the `eg-agent-build windows` console script.
    """

    agent_exe, helper_exe, worker_exe = build_pyinstaller_binaries()

    # Ensure a workers.json exists at project root so it can be installed
    workers_config = PROJECT_ROOT / "workers.json"
    if not workers_config.exists():
        default_config = {
            "workers": [
                {
                    "queue": "default",
                    "workers": 1,
                    "description": "Default queue for general tasks",
                }
            ],
            "notes": [
                "Auto-generated default workers.json during build.",
                "Edit this file in your project to configure queues and workers.",
            ],
        }
        workers_config.write_text(
            json.dumps(default_config, indent=2), encoding="utf-8"
        )

    create_shortcut = os.environ.get(
        "EG_AGENT_CREATE_DESKTOP_SHORTCUT", "true"
    ).lower() == "true"

    wxs_path = generate_wix_wxs(
        agent_exe=agent_exe,
        helper_exe=helper_exe,
        worker_exe=worker_exe,
        workers_config=workers_config,
        create_desktop_shortcut=create_shortcut,
    )
    msi_path = build_msi(wxs_path=wxs_path)
    print(f"[eg-agent-build-windows] Built MSI at: {msi_path}")


if __name__ == "__main__":  # pragma: no cover - CLI entry
    main()
