#!/usr/bin/env bash
set -euo pipefail

echo "Building eg-agent standalone executable for macOS..."

# Python executable
PY_BIN=${PY_BIN:-python3}

# Ensure pyinstaller is available
if ! command -v pyinstaller >/dev/null 2>&1; then
  echo "Error: pyinstaller not found. Install with: pip install pyinstaller" >&2
  exit 1
fi

# Update eg-agent package to latest version in the current environment
if [[ "${EG_AGENT_SELF_UPDATE:-true}" == "true" ]]; then
  $PY_BIN -m pip install --disable-pip-version-check --no-input --upgrade eg-agent
fi

# Entry script (from templates)
ENTRY_SCRIPT="eg_agent/templates/eg_agent_standalone.py"
if [[ ! -f "$ENTRY_SCRIPT" ]]; then
  echo "Error: entry script not found at $ENTRY_SCRIPT" >&2
  exit 1
fi

# Resolve site-packages path via Python to handle venvs reliably
SITE_PACKAGES_DIR="$($PY_BIN - <<'PY'
import site, sys
paths = []
try:
    paths = site.getsitepackages()
except Exception:
    pass
if not paths:
    try:
        paths = [site.getusersitepackages()]
    except Exception:
        pass
if not paths:
    # Fallback to dist-packages
    from sysconfig import get_paths
    paths = [get_paths().get('purelib', '')]
print(paths[0] if paths else '')
PY
)"

if [[ -z "$SITE_PACKAGES_DIR" || ! -d "$SITE_PACKAGES_DIR" ]]; then
  echo "Warning: could not detect site-packages. Continuing without bundling them."
fi

# Clean previous builds
rm -rf dist build _temp_site_packages
mkdir -p _temp_site_packages

# Copy site-packages if found (best effort)
if [[ -n "$SITE_PACKAGES_DIR" && -d "$SITE_PACKAGES_DIR" ]]; then
  echo "Copying site-packages from: $SITE_PACKAGES_DIR"
  rsync -a "$SITE_PACKAGES_DIR/" "_temp_site_packages/" || true
fi

# Build the executable
PYINSTALLER_ARGS=(
  --onefile
  --name eg-agent
  --console
  --add-data "_temp_site_packages:site-packages"
  --hidden-import fastapi
  --hidden-import eg_agent
  --hidden-import uvicorn
  --hidden-import uvicorn.loops
  --hidden-import uvicorn.loops.auto
  --hidden-import uvicorn.protocols
  --hidden-import uvicorn.protocols.http
  --hidden-import uvicorn.protocols.http.auto
  --hidden-import uvicorn.protocols.websockets
  --hidden-import uvicorn.protocols.websockets.auto
  --hidden-import uvicorn.lifespan
  --hidden-import uvicorn.lifespan.on
  --hidden-import click
  --hidden-import anyio
  --hidden-import httptools
  "$ENTRY_SCRIPT"
)

# Optionally bundle local files if present
if [[ -f "tasks.py" ]]; then
  PYINSTALLER_ARGS+=( --add-data "tasks.py:." )
fi
if [[ -f "eg_agent.db" ]]; then
  PYINSTALLER_ARGS+=( --add-data "eg_agent.db:." )
fi

echo "Running pyinstaller..."
pyinstaller "${PYINSTALLER_ARGS[@]}"

# Cleanup temp
rm -rf _temp_site_packages

echo "Build complete! The executable is in the 'dist' directory."
