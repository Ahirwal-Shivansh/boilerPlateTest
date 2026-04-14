# Tasks and Queues Guide

Define and run tasks (immediate or queued) with the eg-agent WebSocket server. For installation and environment variables, see [readme.md](readme.md).

## Overview

- WebSocket-based task execution with real-time status
- **Immediate** tasks: run in the agent process; result over WebSocket
- **Queued** tasks: SQLite-backed (Huey), run by workers; survive restarts
- Auto-update on connect; task recovery for incomplete tasks; optional system tray

## Quick start

1. Create `tasks.py` (see [readme.md](readme.md) for setup and env):

```python
from eg_agent.registry import register_task
import asyncio

@register_task('hello_world')
async def hello_world(name: str = "World") -> str:
    await asyncio.sleep(1)
    return f"Hello, {name}!"
```

2. Start server: `eg-agent run --host 127.0.0.1 --port 8080`
3. Connect to `ws://localhost:8080/ws` and send:

```json
{"type": "run-task", "task_id": "test-1", "task_name": "hello_world", "params": {"name": "Alice"}}
```

## Task registration

- Use `@register_task('name')`; optional: `for_queue=True`, `queue_name="default"`.
- Sync and async supported; sync runs in a threadpool.
- Built-in **ping** task: send `task_name: "ping"`, get `"pong"`.

| Option | Default | Description |
|--------|---------|-------------|
| name | required | Unique task id |
| for_queue | False | Queue by default |
| queue_name | "default" | Default queue |

## Immediate vs queued

- **Immediate**: Fast, in-process; result on WebSocket.
- **Queued**: Persisted; run by `eg-agent-worker <queue>`; good for long or heavy work.

Override per message:

```json
{"type": "run-task", "task_id": "x", "task_name": "my_task", "params": {}, "for_queue": true, "queue_name": "high_priority"}
```

## Queue configuration

Create `queues.py` in your project:

```python
from huey import SqliteHuey
from eg_agent.paths import get_app_path

high_priority = SqliteHuey('high_priority', filename=get_app_path('queues/high_priority.db'))
background = SqliteHuey('background', filename=get_app_path('queues/background.db'))
```

Queues are created on-demand when tasks are enqueued.

## WebSocket protocol

- **Connect**: `ws://localhost:8080/ws`
- **Run task** (client → server):

```json
{"type": "run-task", "task_id": "<id>", "task_name": "<name>", "params": {...}, "for_queue": false, "queue_name": "default"}
```

- **Responses** (server → client):
  - `ack-sent`: task accepted, status running
  - `task-queued`: task queued (queue_name, huey_task_id)
  - `success`: status success, `result` present
  - `task-error`: status error, `message` with error

## Task dequeue

Cancel a queued task before it runs.

- **WebSocket**: `{"type": "dequeue-task", "task_id": "<id>", "queue_name": "optional"}`
- **CLI**: `eg-agent dequeue-task <task_id> --queue <queue_name>`

Only pending/queued tasks can be dequeued.

## Queue priorities and reordering

- Higher numeric priority runs first. New tasks get highest priority (run first).
- Reorder programmatically: `reorder_task_in_queue("default", "task-id", new_position=1)` from `eg_agent.queue`.

## Auto-update

Set `AGENT_VERSION` in `.env`. On WebSocket connect the agent sends `version-check`; server may respond with `latest_version`, `is_required`, and release info. If update required, agent updates and restarts; tasks recover after restart.

## Running the agent and workers

```bash
eg-agent run --host 127.0.0.1 --port 8080   # server (--tray/--no-tray)
eg-agent-worker default --workers 2         # workers per queue
```

## CLI commands (tasks/queues)

| Command | Description |
|---------|-------------|
| `eg-agent run` | Start agent server |
| `eg-agent-worker <queue>` | Start queue worker |
| `eg-agent list-queues` | List queues |
| `eg-agent create_queue <name>` | Create queue |
| `eg-agent dequeue-task <id>` | Cancel queued task |
| `eg-agent db-path` | Database path |
| `eg-agent log-path` | Log path |
| `eg-agent init-db` | Initialize DB |
| `eg-agent migrate-db` | Run migrations |

Full CLI list: [readme.md](readme.md).

## Workflow example

1. `tasks.py`: register immediate and queued tasks (e.g. `hello_world`, `process_data` with `for_queue=True`, `queue_name="background"`).
2. Optional `queues.py`: declare `SqliteHuey` queues with `get_app_path('queues/<name>.db')`.
3. Start: `eg-agent run`; then `eg-agent-worker background --workers 2`.
4. Send run-task over WebSocket (immediate or with `for_queue: true`).

Use JSON-serializable params and return values; use `eg_agent.log_config.logger` in tasks; run workers separately in production.

## Building executables

- **Windows**: `eg-agent scaffold-windows` then run the generated build script.
- **macOS**: `eg-agent scaffold-macos` then run the generated script.

For MSI/PKG installers and hooks: [PACKAGING_GUIDE.md](PACKAGING_GUIDE.md).
