@echo off
echo Building eg-agent standalone executable for Windows...
echo.

REM Activate virtual environment
call venv\Scripts\activate

REM Update eg-agent package to latest version in this venv (default on)
if "%EG_AGENT_SELF_UPDATE%"=="" set EG_AGENT_SELF_UPDATE=true
if /I "%EG_AGENT_SELF_UPDATE%"=="true" (
  python -m pip install --disable-pip-version-check --no-input --upgrade eg-agent
)

REM Clean previous builds
if exist "dist" rmdir /s /q "dist"
if exist "build" rmdir /s /q "build"

REM Create a temporary directory for all site-packages
if exist "_temp_site_packages" rmdir /s /q "_temp_site_packages"
mkdir "_temp_site_packages"

REM Copy ALL site-packages (this ensures we get all dependencies)
xcopy /E /I "venv\Lib\site-packages\*" "_temp_site_packages\" 

REM Build the executable with ALL necessary files
pyinstaller --onefile --name eg-agent --console ^
  --add-data "_temp_site_packages;site-packages" ^
  --add-data "tasks.py;." ^
  --hidden-import=fastapi ^
  --hidden-import=eg_agent ^
  --hidden-import=uvicorn ^
  --hidden-import=uvicorn.loops ^
  --hidden-import=uvicorn.loops.auto ^
  --hidden-import=uvicorn.protocols ^
  --hidden-import=uvicorn.protocols.http ^
  --hidden-import=uvicorn.protocols.http.auto ^
  --hidden-import=uvicorn.protocols.websockets ^
  --hidden-import=uvicorn.protocols.websockets.auto ^
  --hidden-import=uvicorn.lifespan ^
  --hidden-import=uvicorn.lifespan.on ^
  --hidden-import=click ^
  --hidden-import=click.core ^
  --hidden-import=anyio ^
  --hidden-import=anyio._core ^
  --hidden-import=anyio.streams ^
  --hidden-import=asyncio ^
  --hidden-import=asyncio.windows_events ^
  --hidden-import=httptools ^
  --hidden-import=httptools.parser ^
  --hidden-import=uvloop ^
  --hidden-import=uvloop.loop ^
  eg_agent_standalone.py

REM Clean up
rmdir /s /q "_temp_site_packages"

echo.
echo Build complete!
echo Your executable is in the 'dist' folder
echo.
pause

