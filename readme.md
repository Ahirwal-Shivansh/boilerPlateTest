# EG Agent Boilerplate

Local agent with a FastAPI WebSocket server to run registered Python tasks, SQLite task log, and optional system tray. Use it as a base for desktop or server-side agents that talk to a backend over WebSockets.

## Overview

- **Agent server**: FastAPI app with WebSocket at `/ws` for task run requests (async or sync).
- **Task registry**: Register tasks with `@register_task("name")` in `eg_agent/registry`; built-in `ping` returns `"pong"`.
- **Task loading**: On WebSocket connect, the agent loads `tasks.py` from the working directory (or path from `EG_AGENT_TASKS_DIR`). All `@register_task` functions become available.
- **Task lifecycle**: Tasks are logged in SQLite (`agent_tasks`: pending, running, success, error). Incomplete tasks can be recovered on reconnect.
- **Acknowledgements**: Agent sends `ack-sent`, then `success` or `task-error` when done.
- **Tray (optional)**: pystray icon to refresh WebSocket clients or quit. On macOS the tray runs on the main thread.
- **CLI**: `eg-agent` for run, init-db, paths, scaffolding; see [CLI commands](#cli-commands).

## Prerequisites

- **Python** 3.9+
- **OS**: macOS, Windows, or Linux (tray behavior differs by OS)
- Dependencies are installed with the package (FastAPI, Uvicorn, SQLAlchemy, Click, pystray, Pillow, platformdirs, etc.)

## Installation and setup

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
eg-agent init-db
eg-agent db-path            # optional: show DB path
eg-agent log-path           # optional: show log path
```

Configuration can use a `.env` file in the project root; see [Environment variables](#environment-variables).

## Quick start

1. Create `tasks.py` in the directory where you run the agent:

```python
from eg_agent.registry import register_task

@register_task("say_hello")
async def say_hello(name: str = "World"):
    return {"message": f"Hello, {name}!"}
```

2. Start the agent:

```bash
eg-agent run --host 127.0.0.1 --port 8080
```

3. Connect to `ws://127.0.0.1:8080/ws` and send:

```json
{"type": "run-task", "task_id": "test-1", "task_name": "say_hello", "params": {"name": "Alice"}}
```

You get `ack-sent` then `success` with the result. For full message types and queued tasks, see [TASKS_GUIDE.md](TASKS_GUIDE.md).

## Health and WebSocket

- **Health**: `GET http://<host>:<port>/` returns JSON (`active_tasks`, `incomplete_tasks`, `registered_tasks`).
- **WebSocket**: `ws://<host>:<port>/ws`. The server loads `./tasks.py` (or preload + tasks from `EG_AGENT_TASKS_DIR`) on connect.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| **HOST** | 127.0.0.1 | Server bind host |
| **PORT** | 8080 | Server bind port |
| **WORKERS** | 1 | Default worker count for `eg-agent-worker` |
| **WORKERS_CONFIG** | workers.json | Path to workers config file |
| **DB_FILENAME** | eg_agent.db | SQLite DB filename (in app data dir) |
| **LOG_FILENAME** | eg_agent.log | Log filename (in app data dir) |
| **DATABASE_URL** | sqlite:///&lt;path&gt; | Full DB URL (overrides file location) |
| **AGENT_VERSION** | 1.0.0 | Version sent for auto-update checks |
| **EG_AGENT_TASKS_DIR** | — | Override directory for `tasks.py` |
| **EG_AGENT_PRELOAD_FILES** | — | Comma/semicolon/space/newline list of files to preload before loading tasks |
| **EG_AGENT_PRELOAD_MODULES** | — | Alias for EG_AGENT_PRELOAD_FILES |
| **EG_AGENT_TRAY_ICON** | — | Path to tray icon (e.g. icon.icns, icon.png) |
| **AGENT_SERVER_URL** | http://localhost:8080 | Agent URL used by queue/worker code |
| **EG_AGENT_BUILD_ENTRY** | — | Custom entry script when building installers |
| **EG_AGENT_BUILD_VERSION** | 1.0.0 | Installer/product version |
| **EG_AGENT_PKG_NAME** | EG-Agent.pkg | macOS PKG output filename |
| **EG_AGENT_APP_NAME** | eg-agent | App name (paths, executables) |
| **EG_AGENT_CREATE_DESKTOP_SHORTCUT** | true | Create desktop shortcut on macOS install |
| **CODESIGN_IDENTITY** | — | macOS code signing identity for build |
| **LOCALAPPDATA** | — | Windows data root (optional) |

Copy `.env.example` to `.env` and adjust as needed.

## CLI commands

| Command | Description |
|---------|-------------|
| `eg-agent run` | Start the agent server |
| `eg-agent-worker <queue>` | Start a queue worker |
| `eg-agent list-queues` | List discovered queues |
| `eg-agent create_queue <name>` | Create a queue |
| `eg-agent dequeue-task <id>` | Cancel a queued task |
| `eg-agent db-path` | Show database path |
| `eg-agent log-path` | Show log path |
| `eg-agent init-db` | Initialize database |
| `eg-agent migrate-db` | Run DB migrations |
| `eg-agent scaffold-packaging` | Scaffold installer build files |
| `eg-agent scaffold-windows` | Scaffold Windows build script |
| `eg-agent scaffold-macos` | Scaffold macOS build script |

Details for tasks, queues, and WebSocket protocol: [TASKS_GUIDE.md](TASKS_GUIDE.md). Building MSI/PKG installers: [PACKAGING_GUIDE.md](PACKAGING_GUIDE.md).

## Packaging scaffolds

- **Windows**: `eg-agent scaffold-windows` then run the generated build script.
- **macOS**: `eg-agent scaffold-macos` then run the generated script.

For full installer flow (MSI/PKG, hooks, customization): [PACKAGING_GUIDE.md](PACKAGING_GUIDE.md).

## Development

- Reinstall after code changes: `pip install -e .`
- Optional dev tools: `pip install -e .[dev]` then `black eg_agent`, `isort eg_agent`, `flake8 eg_agent`
- Logs: path from `eg-agent log-path`

## Contributing

- Use feature branches; keep commits focused.
- Follow PEP 8; prefer typed APIs and early returns.
- Keep task inputs/outputs JSON-serializable; use `eg_agent.log_config` for logging.
- Use DB helpers in `eg_agent/db.py`; don’t bypass sessions.
- Update this readme when adding commands or behavior.

## Troubleshooting

- **No tasks found**: Create `tasks.py` in the directory where you run `eg-agent run`, or set `EG_AGENT_TASKS_DIR`.
- **Port in use**: Use `--port` or set `PORT` in `.env`.
- **WebSocket not receiving**: Use tray “Refresh WebSocket” or restart the agent; check logs via `eg-agent log-path`.
- **DB issues**: Check path with `eg-agent db-path`; you can remove the SQLite file to reset (development only).
