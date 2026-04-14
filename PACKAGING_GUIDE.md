# Packaging and Installer Guide

Build MSI (Windows) and PKG (macOS) installers for the eg-agent application. Covers scaffold, build flow, install flow, and hooks.

## Quick start

```bash
pip install eg-agent
eg-agent scaffold-packaging
# Optional: edit installer_hooks.py
python build_windows_installer.py   # Windows (needs WiX)
python build_macos_installer.py      # macOS (needs pkgbuild)
```

## What gets scaffolded

After `eg-agent scaffold-packaging`:

```
your-project/
├── installer_helper/
│   ├── __init__.py
│   └── main.py
├── installer_hooks.py    # Your hooks – customize here
├── build_windows_installer.py
└── build_macos_installer.py
```

After building: `dist/` (eg-agent, eg-agent-installer-helper, eg-agent-worker) and `build/windows/EG-Agent.msi` or `build/macos/EG-Agent.pkg`.

## Prerequisites

- **Windows**: Python 3.9+, PyInstaller, [WiX Toolset](https://wixtoolset.org/) (candle, light on PATH)
- **macOS**: Python 3.9+, PyInstaller, Xcode Command Line Tools (`pkgbuild`)

## Build flow

1. **Build PyInstaller binaries**: eg-agent(.exe), eg-agent-installer-helper(.exe), eg-agent-worker(.exe)
2. **Generate WiX/PKG definition**: install binaries to Program Files / Applications; custom action runs installer helper
3. **Compile installer**: Windows → candle + light → EG-Agent.msi; macOS → pkgbuild → EG-Agent.pkg

## Installation flow (when user runs the installer)

1. **Files installed** to system location (e.g. `C:\Program Files\EG\EG-Agent` or `/Applications/EG-Agent/`).
2. **Installer helper runs** (`--full-install`):
   - Stop existing agent if upgrading
   - Call **pre_install()** hook
   - Ensure data directory exists
   - Run database migrations
   - Call **post_install()** hook
   - Start agent
3. Installation complete; agent runs with new version.

## Installer hooks

Define in `installer_hooks.py`; no classes or decorators.

```python
def pre_install(install_root: str, data_root: str, is_upgrade: bool) -> None:
    """Called before database migrations."""
    if is_upgrade:
        print("Upgrading...")

def post_install(install_root: str, data_root: str, is_upgrade: bool) -> None:
    """Called after migrations, before agent starts."""
    print("Setting up configuration...")
```

| Parameter | Description |
|-----------|-------------|
| install_root | Binary install path (e.g. Program Files) |
| data_root | Data path (DB, config; e.g. AppData / Application Support) |
| is_upgrade | True if upgrade, False if fresh install |

**Example – backup DB on upgrade:**

```python
def pre_install(install_root, data_root, is_upgrade):
    if is_upgrade:
        import shutil
        from pathlib import Path
        db = Path(data_root) / "eg_agent.db"
        if db.exists():
            shutil.copy(db, db.with_suffix(".backup"))
```

**Example – default config after install:**

```python
def post_install(install_root, data_root, is_upgrade):
    import json
    from pathlib import Path
    config_path = Path(data_root) / "config.json"
    if not config_path.exists():
        config_path.write_text(json.dumps({"api_url": "https://api.example.com"}, indent=2))
```

If a hook raises, installation stops; Windows MSI may roll back. Data in `data_root` is not deleted.

## Customization

- **Version**: In build script, set `product_version="2.0.0"` in `generate_wix_wxs()` (or equivalent in macOS script).
- **Product name**: Edit the WiX `<Product Name="...">` or macOS app name in the build script.
- **Custom entry point**: `export EG_AGENT_BUILD_ENTRY=my_app.py` before building.
- **Workers**: Create `workers.json` in project root (e.g. `{"workers": [{"queue": "default", "workers": 1}]}`); it is included in the installer.

See [readme.md](readme.md) for env vars (e.g. `EG_AGENT_BUILD_VERSION`, `EG_AGENT_APP_NAME`, `EG_AGENT_PKG_NAME`, `EG_AGENT_CREATE_DESKTOP_SHORTCUT`).

## Build commands

```bash
python build_windows_installer.py   # → build/windows/EG-Agent.msi
python build_macos_installer.py     # → build/macos/EG-Agent.pkg
# Or: eg-agent-build windows / eg-agent-build macos
```

## Installation locations

| OS | Binaries | Data |
|----|----------|------|
| Windows | `C:\Program Files\EG\EG-Agent\` | `%LOCALAPPDATA%\EG-Agent\` |
| macOS | `/Applications/EG-Agent/` | `~/Library/Application Support/EG-Agent/` |

## Troubleshooting

- **candle not found**: Install WiX Toolset and add to PATH.
- **pkgbuild not found**: Run `xcode-select --install`.
- **Hook not called**: Ensure `installer_hooks.py` is next to the build script; function names exactly `pre_install` and `post_install`; no syntax errors.
- **Agent doesn’t start after install**: Check Event Viewer (Windows) or Console (macOS); verify DB migrations; run agent manually to see errors.
