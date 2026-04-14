import os
import sys
import json
import inspect
import asyncio
import platform
import time
from pathlib import Path
from typing import Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi import Depends
from sqlalchemy.orm import Session
from starlette.websockets import (
    WebSocketState, WebSocketDisconnect as StarletteWebSocketDisconnect
)

from eg_agent.registry import task_registry, get_task
from eg_agent.queue import TaskExecutor
from eg_agent.loader import load_user_tasks
from eg_agent.db import (get_db,
                         log_task,
                         update_task_status,
                         TaskStatus,
                         SessionLocal,
                         mark_received_ack,
                         mark_sent_ack,
                         update_huey_task_id,
                         TaskLog,
                         get_global_key,
                         set_global_key)
from eg_agent.constants import (AgentCallsConstants,
                                UpdateCallsConstants,
                                ActivationConstants,
                                ApplicationPlatformConstants)
from eg_agent.log_config import logger as base_logger
from eg_agent.auto_updater import get_updater, init_updater


logger = base_logger.getChild("server")

app = FastAPI(title="EG Agent", version="0.1.0")

# Get version from .env file (AGENT_VERSION set by application developer)
AGENT_VERSION = os.getenv("AGENT_VERSION", "1.0.0")

# Global variable to track active tasks
active_tasks: Dict[str, asyncio.Task] = {}
# Track tasks currently being recovered to avoid duplicate restarts
recovering_tasks: "set[str]" = set()

# Track connected websocket clients and event loop for cross-thread control
connected_clients: "set[WebSocket]" = set()
ws_by_id: Dict[int, WebSocket] = {}
server_event_loop: "asyncio.AbstractEventLoop | None" = None

# Track sent responses per websocket to prevent duplicates per connection
sent_responses_by_ws: Dict[int, set[str]] = {}
# Track task IDs per websocket to avoid cancelling unrelated tasks
ws_to_task_ids: Dict[int, set[str]] = {}
# Track owning websocket per task to avoid broadcasting to all connections
task_owner_ws: Dict[str, int] = {}
# Queue undelivered messages per task to replay on reconnect
pending_task_messages: Dict[str, list[dict]] = {}


async def _send_response_once(ws_connection, message: dict,
                              response_key: str = None):
    """Send a response only once per websocket connection."""
    try:
        if ws_connection.client_state != WebSocketState.CONNECTED:
            logger.warning("Skip send: websocket not connected")
            return False
    except Exception as e:
        logger.warning(f"Skip send: websocket state unavailable: {e}")
        return False
    ws_id = id(ws_connection)
    sent_for_ws = sent_responses_by_ws.setdefault(ws_id, set())
    if response_key is None:
        msg_type = message.get('type', 'unknown')
        task_id = message.get('task_id', 'unknown')

        # For progress messages, allow multiple sends by including stage/step
        if msg_type == 'task-progress':
            stage = message.get('stage', 'unknown')
            step = message.get('step', '')
            response_key = f"{task_id}_{msg_type}_{stage}_{step}"
        else:
            response_key = f"{task_id}_{msg_type}"

    if response_key in sent_for_ws:
        logger.warning(f"Duplicate response prevented for key: {response_key}")
        return False

    sent_for_ws.add(response_key)
    try:
        await ws_connection.send_text(json.dumps(message))
        return True
    except (WebSocketDisconnect, StarletteWebSocketDisconnect) as e:
        logger.warning(f"WebSocket disconnected during send: {e}")
        sent_for_ws.discard(response_key)
        return False
    except RuntimeError as e:
        # Typical when a close frame has already been sent
        logger.warning(f"RuntimeError during WS send (likely closed): {e}")
        sent_for_ws.discard(response_key)
        return False
    except Exception as e:
        # Unexpected send error – log but don't crash the task
        logger.error(f"Unexpected error sending WS message: {e}")
        sent_for_ws.discard(response_key)
        return False


async def _send_with_fallback(message: dict, ws_connection):
    """
    Send to the current websocket if connected, otherwise try any subscribed
    websockets for this task_id. This prevents loss after refresh.
    """
    if (ws_connection
            and ws_connection.client_state == WebSocketState.CONNECTED):
        return await _send_response_once(ws_connection, message)

    # Fallback delivery if we lost the original socket
    task_id = message.get("task_id")
    if not task_id:
        logger.warning("Fallback send without task_id; dropping")
        return False

    delivered = False

    # Prefer the owner websocket if recorded
    owner_id = task_owner_ws.get(task_id)
    if owner_id:
        ws = ws_by_id.get(owner_id)
        if ws:
            try:
                sent = await _send_response_once(ws, message)
                if sent:
                    return True
            except Exception as e:
                logger.error(
                    f"Failed to send progress to owner websocket: {e}")
        # Owner missing or not connected; drop mapping and try others
        task_owner_ws.pop(task_id, None)

    # Fallback: send to all sockets subscribed to this task
    for ws in list(connected_clients):
        ws_tasks = ws_to_task_ids.get(id(ws), set())
        if task_id in ws_tasks:
            try:
                await _send_response_once(ws, message)
                delivered = True
            except Exception as e:
                logger.error(f"Failed to send progress to websocket: {e}")

    if not delivered:
        pending_task_messages.setdefault(task_id, []).append(message)
        logger.info(
            f"No active websocket found for task_id {task_id} to send progress"
        )
    return delivered


async def _send_progress_to_task_clients(message: dict):
    """Forward a progress-like message to websockets subscribed to the task."""
    # Reuse the same fallback logic; we don't have a specific ws here
    return await _send_with_fallback(message, ws_connection=None)


async def _close_connected_clients():
    """Close all currently connected websocket clients."""
    clients_snapshot = list(connected_clients)
    for client in clients_snapshot:
        try:
            await client.close()
        except Exception as e:
            logger.error(f"Failed to close WebSocket client: {str(e)}")


def refresh_websocket_connections() -> bool:
    """
    Trigger a refresh by closing all current WebSocket connections.

    Returns True if a server loop was present and the refresh was scheduled.
    """
    if server_event_loop is None:
        logger.warning("No server event loop available to refresh websockets")
        return False

    try:
        asyncio.run_coroutine_threadsafe(_close_connected_clients(),
                                         server_event_loop)
        logger.info("Scheduled WebSocket clients refresh")
        return True
    except Exception as e:
        logger.error(f"Failed to schedule websocket refresh: {str(e)}")
        return False


async def _poll_queued_task_and_notify(task_id: str, ws):
    """Poll the DB for a queued task and notify over WS on completion."""
    try:
        while True:
            db = SessionLocal()
            try:
                task = db.query(TaskLog).filter(
                    TaskLog.task_id == task_id).first()
                if task is None:
                    await asyncio.sleep(0.5)
                    continue

                if task.status == TaskStatus.SUCCESS:
                    completed_message = {
                        "type": "success",
                        "task_id": task_id,
                        "status": TaskStatus.SUCCESS,
                        "result": task.result,
                    }
                    await _send_response_once(ws, completed_message)
                    logger.info(
                        f"Queued task {task_id} completed successfully")
                    break

                if task.status == TaskStatus.ERROR:
                    error_message = {
                        "type": AgentCallsConstants.TASK_ERROR_CALL,
                        "task_id": task_id,
                        "status": TaskStatus.ERROR,
                        "message": task.result,
                    }
                    await _send_response_once(ws, error_message)
                    logger.error(
                        f"Queued task {task_id} failed: {task.result}")
                    break
            finally:
                db.close()

            await asyncio.sleep(0.5)
    except Exception as e:
        logger.error(f"Polling error for queued task {task_id}: {str(e)}")
    finally:
        active_tasks.pop(task_id, None)
        ws_id = id(ws)
        if ws_id in ws_to_task_ids and task_id in ws_to_task_ids[ws_id]:
            ws_to_task_ids[ws_id].discard(task_id)


async def run_task(task_func, task_id, task_name,
                   params, ws_connection):
    """Run one task in background with its own DB session."""
    db = SessionLocal()
    try:
        logger.info(f"Starting task {task_id} "
                    +
                    f"({task_name})")
        # Update task status to RUNNING
        update_task_status(db, task_id, TaskStatus.RUNNING)

        # Prepare optional websocket send helpers injected into tasks.
        loop = asyncio.get_running_loop()

        async def ws_send_async(message: dict):
            """Send over websocket from async tasks (with fallback)."""
            # Auto-inject task_id and task_name if not present
            if 'task_id' not in message:
                message = {**message, 'task_id': task_id}
            if 'task_name' not in message:
                message = {**message, 'task_name': task_name}
            await _send_with_fallback(message, ws_connection)
            await asyncio.sleep(0)

        def ws_send_sync(message: dict):
            """Send over websocket from sync tasks running in executor."""
            # Auto-inject task_id and task_name if not present
            if 'task_id' not in message:
                message = {**message, 'task_id': task_id}
            if 'task_name' not in message:
                message = {**message, 'task_name': task_name}
            asyncio.run_coroutine_threadsafe(
                _send_with_fallback(message, ws_connection),
                loop
            )
            time.sleep(0)
        # Inject ws_send into params only if the task accepts it.
        sig = inspect.signature(task_func)
        if "ws_send" in sig.parameters:
            if inspect.iscoroutinefunction(task_func):
                params = {**params, "ws_send": ws_send_async}
            else:
                params = {**params, "ws_send": ws_send_sync}

        # Execute the task
        if inspect.iscoroutinefunction(task_func):
            result = await task_func(**params)
        else:
            result = await loop.run_in_executor(None,
                                                lambda: task_func(**params))

        logger.info(f"Task {task_id} ({task_name}) "
                    +
                    "completed successfully.")
        # Update task status to SUCCESS
        update_task_status(db, task_id,
                           TaskStatus.SUCCESS, result)

        # Send completion message to Django (with fallback on reconnect)
        completion_message = {
            "type": "success",
            "status": "success",
            "task": task_name,
            "task_id": task_id,
            "result": result
        }
        await _send_with_fallback(completion_message, ws_connection)

    except Exception as e:
        # Handle errors
        error_msg = str(e)
        logger.error(f"Task {task_id} ({task_name}) "
                     +
                     f"failed with error: {error_msg}")
        update_task_status(db, task_id,
                           TaskStatus.ERROR, error_msg)

        # Send error message to Django (with fallback on reconnect)
        error_message = {
            "type": "task-error",
            "status": "error",
            "task": task_name,
            "task_id": task_id,
            "message": error_msg
        }
        await _send_with_fallback(error_message, ws_connection)
    finally:
        db.close()
        # Remove task from active tasks
        if task_id in active_tasks:
            del active_tasks[task_id]
            logger.info(f"Task {task_id} removed from active_tasks")


async def recover_incomplete_tasks(ws_connection):
    """Recover tasks that were not completed before agent crash"""
    logger.debug("Entering recover_incomplete_tasks")
    db = SessionLocal()
    recovery_count = 0

    try:
        logger.info("Starting recovery of incomplete tasks")
        # Find all tasks that need recovery (excluding completed ones)
        incomplete_tasks = db.query(TaskLog).filter(
            (TaskLog.status != TaskStatus.SUCCESS) &
            (TaskLog.status != TaskStatus.ERROR)
        ).all()
        logger.debug(f"Found {len(incomplete_tasks)} incomplete tasks")

        recovery_count = len(incomplete_tasks)

        if recovery_count > 0:
            logger.info(f"Found {recovery_count} incomplete tasks to recover")
            for task in incomplete_tasks:
                logger.debug(
                    f"Recovering task {task.task_id} with status {task.status}"
                )
                try:
                    # Handle different states appropriately
                    if task.status == TaskStatus.RUNNING:
                        logger.info(
                            "Recovering RUNNING task %s (%s)",
                            task.task_id,
                            task.task_name,
                        )

                        # If already active/recovering, bind this websocket
                        if task.task_id in active_tasks:
                            ws_to_task_ids[id(ws_connection)].add(task.task_id)
                            task_owner_ws[task.task_id] = id(ws_connection)
                            logger.info(
                                "Task %s already active; bound websocket",
                                task.task_id,
                            )
                            continue
                        if task.task_id in recovering_tasks:
                            ws_to_task_ids[id(ws_connection)].add(task.task_id)
                            logger.info(
                                "Task %s already recovering; bound websocket",
                                task.task_id,
                            )
                            continue

                        # Parse parameters and restart task
                        params = json.loads(task.params) if task.params else {}
                        task_info = get_task(task.task_name)
                        task_func = (task_info["function"]
                                     if task_info else None)

                        if task.for_queue:
                            logger.debug(
                                "Task %s marked for queue recovery",
                                task.task_id,
                            )
                            continue  # Queue worker will handle it
                        if task_func:
                            recovering_tasks.add(task.task_id)
                            task_obj = asyncio.create_task(
                                run_task(task_func,
                                         task.task_id,
                                         task.task_name,
                                         params,
                                         ws_connection)
                            )
                            active_tasks[task.task_id] = task_obj
                            task_owner_ws[task.task_id] = id(ws_connection)
                            recovering_tasks.discard(task.task_id)
                            logger.info(
                                f"Task {task.task_id} ({task.task_name}) "
                                + "restarted"
                            )
                        else:
                            logger.error(f"Task function '{task.task_name}' "
                                         +
                                         "not found after restart")
                            # Notify Django about the missing task function
                            error_message = {
                                "type": "task-error",
                                "task_id": task.task_id,
                                "task_name": task.task_name,
                                "message": (f"Task function '{task.task_name}'"
                                            + " not found after restart")
                            }
                            await ws_connection.send_text(
                                json.dumps(error_message)
                            )

                    elif task.status == TaskStatus.PENDING:
                        logger.info(
                            "Recovering PENDING task "
                            + f"{task.task_id} ({task.task_name})"
                        )
                        logger.debug(
                            "PENDING task %s, sent_ack=%s",
                            task.task_id,
                            task.sent_ack,
                        )
                        if task.for_queue:
                            logger.debug(
                                "Task %s marked for queue recovery",
                                task.task_id,
                            )
                            continue  # Queue worker will handle it

                        # If already active or recovering, just bind to this ws
                        if task.task_id in active_tasks:
                            ws_to_task_ids[id(ws_connection)].add(task.task_id)
                            task_owner_ws[task.task_id] = id(ws_connection)
                            logger.info(
                                "Pending task %s already active; "
                                "bound websocket",
                                task.task_id,
                            )
                            continue
                        if task.task_id in recovering_tasks:
                            ws_to_task_ids[id(ws_connection)].add(task.task_id)
                            logger.info(
                                "Pending task %s already recovering; "
                                "bound websocket",
                                task.task_id,
                            )
                            continue
                        if not task.sent_ack:
                            params = (json.loads(task.params) if task.params
                                      else {})
                            task_message = {
                                "type": AgentCallsConstants.ACK_SENT_CALL,
                                "task_id": task.task_id,
                                "task_name": task.task_name,
                                "status": TaskStatus.RUNNING,
                                "params": params
                            }
                            task_func = task_registry.get(task.task_name)

                            if task_func:
                                recovering_tasks.add(task.task_id)
                                task_obj = asyncio.create_task(
                                    run_task(task_func,
                                             task.task_id,
                                             task.task_name,
                                             params,
                                             ws_connection)
                                )
                                active_tasks[task.task_id] = task_obj
                                task_owner_ws[task.task_id] = id(ws_connection)
                                recovering_tasks.discard(task.task_id)
                                logger.info(f"Task {task.task_id} "
                                            +
                                            f"({task.task_name}) "
                                            +
                                            "restarted from PENDING"
                                            )
                            logger.debug(
                                "Sending ACK for PENDING %s",
                                task.task_id,
                            )
                            await ws_connection.send_text(
                                json.dumps(task_message)
                            )
                            mark_sent_ack(db, task.task_id)
                            logger.info(
                                "Sent ACK for recovered PENDING task %s",
                                task.task_id,
                            )
                        else:
                            # Already sent to Django; querying status
                            logger.debug(
                                (
                                    "PENDING task %s already sent ACK; "
                                    "querying status"
                                ),
                                task.task_id,
                            )
                            status_query = {
                                "type": "task-status-query",
                                "task_id": task.task_id,
                                "status": task.status,
                                "task_name": task.task_name
                            }
                            await ws_connection.send_text(
                                json.dumps(status_query))

                    elif task.status is TaskStatus.ERROR:
                        logger.warning("Found failed task: "
                                       +
                                       f"{task.task_id} ({task.task_name})")
                        logger.debug(
                            "ERROR task %s, resending error notification",
                            task.task_id,
                        )
                        # Resend the error notification to Django
                        error_message = {
                            "type": "task-error",
                            "task_id": task.task_id,
                            "task_name": task.task_name,
                            "message": ("Task failed before "
                                        +
                                        f"crash: {task.result}")
                        }
                        await ws_connection.send_text(
                            json.dumps(error_message)
                        )
                        logger.info("Resent error notification "
                                    + f"for failed task {task.task_id}")

                except Exception as e:
                    logger.error("Failed to recover task "
                                 +
                                 f"{task.task_id if task else 'unknown'}: "
                                 +
                                 f"{str(e)}")
                    # Send error to Django for this recovery failure
                    error_message = {
                        "type": "recovery-error",
                        "task_id": task.task_id if task else "unknown",
                        "message": f"Failed to recover task: {str(e)}"
                    }
                    await ws_connection.send_text(json.dumps(error_message))

        logger.debug(f"Recovery count: {recovery_count}")
        return recovery_count

    except Exception as e:
        logger.error(f"Error during task recovery: {str(e)}")
        # Send general recovery error to Django
        error_message = {
            "type": "recovery-error",
            "message": f"Task recovery process failed: {str(e)}"
        }
        await ws_connection.send_text(json.dumps(error_message))
        return 0
    finally:
        logger.debug("Closing DB session in recover_incomplete_tasks")
        db.close()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, db: Session = Depends(get_db)):
    await ws.accept()
    logger.info("WebSocket connected")

    global server_event_loop
    if server_event_loop is None:
        server_event_loop = asyncio.get_running_loop()
    connected_clients.add(ws)
    ws_by_id[id(ws)] = ws
    ws_to_task_ids[id(ws)] = set()

    # Check activation key - agent will not run until key is set
    activation_pending = False
    activation_key = get_global_key(
        db, ActivationConstants.ACTIVATION_KEY_NAME)
    if not activation_key:
        activation_pending = True
        sys_os = platform.system()
        os_value = (
            ApplicationPlatformConstants.WINDOWS if sys_os == "Windows"
            else ApplicationPlatformConstants.MACOS if sys_os == "Darwin"
            else ApplicationPlatformConstants.LINUX if sys_os == "Linux"
            else None
        )
        activation_required_msg = {
            "type": ActivationConstants.ACTIVATION_REQUIRED,
            "status": "pending",
            "message": "Please provide activation key to activate the agent",
            "operating_system": os_value
        }
        await ws.send_text(json.dumps(activation_required_msg))
        logger.info("Activation key not found; sent activation-required")

    load_user_tasks()
    logger.info("User tasks loaded")

    # Initialize updater and send version check BEFORE task recovery
    updater = get_updater()
    if not updater:
        updater = init_updater(AGENT_VERSION)

    # Send version-check message on connection established
    version_check_msg = updater.create_version_check_message()
    await ws.send_text(json.dumps(version_check_msg))
    logger.info("Sent version-check: %s", AGENT_VERSION)

    # Wait for version-check-response to check for mandatory updates
    mandatory_update_pending = False
    try:
        # Wait for version check response (timeout after 10 seconds)
        raw_data = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
        payload = json.loads(raw_data)

        if (payload.get("type") == ActivationConstants.ACTIVATION_KEY
                and activation_pending):
            key_value = payload.get(ActivationConstants.ACTIVATION_KEY_NAME)
            if key_value:
                try:
                    set_global_key(db, ActivationConstants.ACTIVATION_KEY_NAME,
                                   str(key_value))
                    activation_pending = False
                    success_msg = {
                        "type": ActivationConstants.ACTIVATION_SUCCESS,
                        "status": "activated",
                        "message": "Activation key set successfully"
                    }
                    await ws.send_text(json.dumps(success_msg))
                    logger.info("Activation key received and saved (init)")
                except Exception as e:
                    logger.error(f"Failed to save activation key: {e}")

        elif (payload.get("type")
              ==
              UpdateCallsConstants.VERSION_CHECK_RESPONSE):
            if activation_pending:
                key_value = payload.get(
                    ActivationConstants.ACTIVATION_KEY_NAME)
                if key_value:
                    try:
                        set_global_key(
                            db,
                            ActivationConstants.ACTIVATION_KEY_NAME,
                            str(key_value))
                        activation_pending = False
                        success_msg = {
                            "type": ActivationConstants.ACTIVATION_SUCCESS,
                            "status": "activated",
                            "message": "Activation key set successfully"
                        }
                        await ws.send_text(json.dumps(success_msg))
                        logger.info(
                            "Activation key received with version-check"
                        )
                    except Exception as e:
                        logger.error(f"Failed to save activation key: {e}")

            needs_update = updater.handle_version_check_response(payload)
            if needs_update and updater.is_required:
                # Mandatory update - skip task recovery, update first
                logger.warning(
                    "MANDATORY UPDATE required: %s -> %s",
                    updater.current_version,
                    updater.latest_version,
                )
                mandatory_update_pending = True

                # Send download request immediately
                download_msg = updater.create_download_request_message()
                await ws.send_text(json.dumps(download_msg))
                logger.info("Sent download request for mandatory update")

            elif needs_update:
                # Optional update - will handle after task recovery
                logger.info(
                    "Optional update available: %s -> %s",
                    updater.current_version,
                    updater.latest_version,
                )
                # Send download request (will be handled in message loop)
                download_msg = updater.create_download_request_message()
                await ws.send_text(json.dumps(download_msg))
        else:
            # Not a version check response, will be handled in main loop
            logger.debug("Received non-version message during init")

    except asyncio.TimeoutError:
        logger.warning("Version check timed out, proceeding without update")
    except json.JSONDecodeError:
        logger.error("Invalid JSON in version check response")
    except Exception as e:
        logger.error("Error during version check: %s", e)

    if not mandatory_update_pending and not activation_pending:
        recovery_count = await recover_incomplete_tasks(ws)
        if recovery_count > 0:
            logger.info(f"Recovery completed: {recovery_count} tasks restored")
    elif activation_pending:
        logger.info("Skipping task recovery - activation key required")
    else:
        logger.info("Skipping task recovery - mandatory update pending")

    try:
        while True:
            raw_data = await ws.receive_text()
            if len(raw_data) <= 400:
                display_data = raw_data
            else:
                display_data = raw_data[:400] + "...[truncated]"
            logger.info(f"Received: {display_data}")

            try:
                payload = json.loads(raw_data)
            except json.JSONDecodeError:
                logger.error("Invalid JSON received")
                await ws.send_text(json.dumps({
                    "type": AgentCallsConstants.TASK_ERROR_CALL,
                    "status": "error",
                    "message": "Invalid JSON"
                }))
                continue

            for_queue = payload.get("for_queue", False)
            queue_name = payload.get("queue_name")
            request_type = payload.get("type")
            task_name = payload.get("task_name")
            task_id = payload.get("task_id")
            params = payload.get("params", {})

            # When activation pending, only process activation-key messages
            if activation_pending:
                if request_type == ActivationConstants.ACTIVATION_KEY:
                    key_value = payload.get(
                        ActivationConstants.ACTIVATION_KEY_NAME)
                    if key_value:
                        try:
                            set_global_key(
                                db,
                                ActivationConstants.ACTIVATION_KEY_NAME,
                                str(key_value))
                            activation_pending = False
                            success_msg = {
                                "type": ActivationConstants.ACTIVATION_SUCCESS,
                                "status": "activated",
                                "message": "Activation key set successfully"
                            }
                            await ws.send_text(json.dumps(success_msg))
                            logger.info("Activation key received and saved")
                        except Exception as e:
                            logger.error(f"Failed to save activation key: {e}")
                            await ws.send_text(json.dumps({
                                "type": AgentCallsConstants.TASK_ERROR_CALL,
                                "status": "error",
                                "message": f"Failed to save activation key: {e}"  # noqa: E501
                            }))
                    else:
                        await ws.send_text(json.dumps({
                            "type": AgentCallsConstants.TASK_ERROR_CALL,
                            "status": "error",
                            "message": "activation_key value required"
                        }))
                continue  # Skip all other processing until activated

            if request_type == "run-task":
                if not task_id or not task_name:
                    logger.error(
                        "Missing task_id or task_name in run-task request")
                    await ws.send_text(json.dumps({
                        "type": AgentCallsConstants.TASK_ERROR_CALL,
                        "status": "error",
                        "message": "Missing task_id or task_name"
                    }))
                    continue

                # Use the new get_task function that returns task metadata
                task_info = get_task(task_name)
                if not task_info:
                    logger.error(f"No task '{task_name}' found in registry")
                    await ws.send_text(json.dumps({
                        "type": AgentCallsConstants.TASK_ERROR_CALL,
                        "status": "error",
                        "message": f"No task '{task_name}' found"
                    }))
                    continue

                task_func = task_info['function']

                # Determine if task should go to queue
                # (message flag OR task configuration)
                should_queue = for_queue or task_info.get('for_queue', False)
                actual_queue_name = (queue_name
                                     if queue_name
                                     else
                                     task_info.get('queue_name', 'default'))

                # Log the task
                try:
                    log_task(db, task_id, task_name, json.dumps(params),
                             for_queue=should_queue,
                             queue_name=actual_queue_name)
                    mark_sent_ack(db, task_id)
                    logger.info(f"Task {task_id} logged in agent DB")
                except Exception as e:
                    logger.error(f"Failed to log task {task_id}: {str(e)}")
                    await ws.send_text(json.dumps({
                        "type": AgentCallsConstants.TASK_ERROR_CALL,
                        "status": "error",
                        "message": f"Failed to log task: {str(e)}"
                    }))
                    continue

                # Send ACK immediately (this happens before queueing)
                ack_message = {
                    "type": AgentCallsConstants.ACK_SENT_CALL,
                    "task": task_name,
                    "task_id": task_id,
                    "status": TaskStatus.RUNNING
                }
                await ws.send_text(json.dumps(ack_message))
                logger.info(f"ACK sent for task {task_id}")

                if should_queue:
                    try:
                        # Enqueue the task using the new TaskExecutor
                        huey_task = TaskExecutor.enqueue_task(
                            task_name, task_func, params,
                            task_id, actual_queue_name
                        )

                        # Store the huey_task_id in the database
                        update_huey_task_id(db, task_id, huey_task.id)

                        # Send queued acknowledgement
                        queued_message = {
                            "type": "task-queued",
                            "task_id": task_id,
                            "queue_name": actual_queue_name,
                            "huey_task_id": huey_task.id,
                            "status": "queued",
                            "message": f"Task {task_name} queued successfully"
                        }
                        await ws.send_text(json.dumps(queued_message))
                        logger.info(f"Task {task_id} added to queue "
                                    + f"'{actual_queue_name}' with Huey "
                                    + f"ID: {huey_task.id}")

                        # Start polling DB for completion
                        # to send WS success/error
                        poller = asyncio.create_task(
                            _poll_queued_task_and_notify(task_id, ws)
                        )
                        active_tasks[task_id] = poller
                        ws_to_task_ids[id(ws)].add(task_id)
                        task_owner_ws[task_id] = id(ws)

                    except Exception as e:
                        logger.error("Failed to enqueue task "
                                     + f"{task_id}: {str(e)}")
                        # Update task status to error in database
                        update_task_status(db, task_id, TaskStatus.ERROR,
                                           result=str(e))

                        error_message = {
                            "type": AgentCallsConstants.TASK_ERROR_CALL,
                            "task_id": task_id,
                            "status": "error",
                            "message": f"Failed to enqueue task: {str(e)}"
                        }
                        await ws.send_text(json.dumps(error_message))

                else:
                    # Immediate execution for non-queued tasks
                    # Execute the task immediately
                    task = asyncio.create_task(
                        run_task(task_func, task_id,
                                 task_name, params, ws)
                    )
                    active_tasks[task_id] = task
                    ws_to_task_ids[id(ws)].add(task_id)
                    task_owner_ws[task_id] = id(ws)
                    logger.info(f"Task {task_id} started in background")

            elif request_type == AgentCallsConstants.ACK_RECEIVED_CALL:
                task_id = payload.get("task_id")
                if task_id:
                    try:
                        mark_received_ack(db, task_id)
                        logger.info(
                            f"Received ACK confirmation for task {task_id}")
                    except Exception as e:
                        logger.error(
                            f"Failed to mark ACK for {task_id}: {str(e)}")
                else:
                    logger.warning("Received ACK without task_id")

            elif request_type == "task-status-query":
                task_id = payload.get("task_id")
                status = payload.get("status")
                task_name = payload.get("task_name")

                if status == TaskStatus.RUNNING:
                    # Start the task using asyncio, no acknowledgement is sent
                    task_info = get_task(task_name)
                    if task_info:
                        task_func = task_info['function']
                        task_obj = asyncio.create_task(
                            run_task(task_func, task_id, task_name, params, ws)
                        )
                        active_tasks[task_id] = task_obj
                        ws_to_task_ids[id(ws)].add(task_id)
                        task_owner_ws[task_id] = id(ws)
                        logger.info(f"Task {task_id} ({task_name}) started in "
                                    + "response to status query (RUNNING)")
                    else:
                        logger.error(
                            f"Task function '{task_name}' not found when "
                            + "handling status RUNNING in status-query")

            elif request_type == "dequeue-task":
                task_id = payload.get("task_id")
                queue_name = payload.get("queue_name")

                if not task_id:
                    logger.error("Missing task_id in dequeue-task request")
                    await ws.send_text(json.dumps({
                        "type": "dequeue-error",
                        "status": "error",
                        "message": "Missing task_id"
                    }))
                    continue

                try:
                    result = TaskExecutor.dequeue_task(task_id, queue_name)

                    if result["success"]:
                        if task_id in active_tasks:
                            active_tasks[task_id].cancel()
                            del active_tasks[task_id]
                            logger.info(f"Cancelled polling for dequeued task "
                                        f"{task_id}")

                        ws_id = id(ws)
                        if (ws_id in ws_to_task_ids and
                                task_id in ws_to_task_ids[ws_id]):
                            ws_to_task_ids[ws_id].discard(task_id)

                        dequeue_message = {
                            "type": "task-dequeued",
                            "task_id": task_id,
                            "task_name": result.get("task_name"),
                            "queue_name": result.get("queue_name"),
                            "huey_task_id": result.get("huey_task_id"),
                            "status": "cancelled",
                            "message": result["message"]
                        }
                        await ws.send_text(json.dumps(dequeue_message))
                        logger.info(f"Task {task_id} dequeued successfully")
                    else:
                        error_message = {
                            "type": "dequeue-error",
                            "task_id": task_id,
                            "status": "error",
                            "message": result["message"]
                        }
                        await ws.send_text(json.dumps(error_message))
                        logger.warning(f"Failed to dequeue task {task_id}: "
                                       f"{result['message']}")

                except Exception as e:
                    logger.error(f"Exception during dequeue for task "
                                 f"{task_id}: {str(e)}")
                    await ws.send_text(json.dumps({
                        "type": "dequeue-error",
                        "task_id": task_id,
                        "status": "error",
                        "message": f"Dequeue failed: {str(e)}"
                    }))

            # Auto-update message handlers
            elif request_type == UpdateCallsConstants.VERSION_CHECK_RESPONSE:
                updater = get_updater()
                if updater:
                    needs_update = updater.handle_version_check_response(
                        payload)
                    if needs_update:
                        # Version mismatch - request download
                        action = ("rollback"
                                  if updater.is_rollback()
                                  else "update")
                        logger.info(
                            "%s needed: %s -> %s",
                            action.capitalize(),
                            updater.current_version,
                            updater.latest_version,
                        )
                        # Send download request
                        msg = updater.create_download_request_message()
                        await ws.send_text(json.dumps(msg))
                        logger.info("Sent download request")

            elif request_type == UpdateCallsConstants.DOWNLOAD_RESPONSE:
                updater = get_updater()
                if updater and updater.handle_download_response(payload):
                    # Download URL received - start download in background
                    asyncio.create_task(_handle_update_download(updater, ws))

    except WebSocketDisconnect:
        logger.warning("WebSocket disconnected")
        # Cancel running tasks bound to this websocket so they can be
        # cleanly restarted on next recovery.
        ws_id = id(ws)
        for task_id in list(ws_to_task_ids.get(ws_id, set())):
            task = active_tasks.get(task_id)
            if task:
                task.cancel()
                active_tasks.pop(task_id, None)
                logger.info(
                    f"Cancelled task {task_id} due to websocket disconnect")
    except Exception as e:
        logger.error(f"Unexpected error in WebSocket handler: {str(e)}")
        # Avoid cancelling all tasks; let them run to completion.
        raise
    finally:
        if ws in connected_clients:
            connected_clients.discard(ws)
        ws_id = id(ws)
        # Clear owner mappings for tasks tied to this websocket
        for task_id in ws_to_task_ids.get(ws_id, set()):
            if task_owner_ws.get(task_id) == ws_id:
                task_owner_ws.pop(task_id, None)
        ws_to_task_ids.pop(ws_id, None)
        ws_by_id.pop(ws_id, None)
        sent_responses_by_ws.pop(id(ws), None)


async def _handle_update_download(updater, ws):
    """Handle update download, verify, and apply in background."""
    is_mandatory = updater.is_required

    try:
        if is_mandatory:
            logger.warning("Processing MANDATORY update...")

        # Notify download starting
        await ws.send_text(json.dumps({
            "type": "update-downloading",
            "version": updater.latest_version,
            "is_required": is_mandatory,
        }))

        # Download
        download_path = await updater.download_update()
        if not download_path:
            logger.error("Download failed")
            await ws.send_text(json.dumps({
                "type": "update-error",
                "message": "Download failed",
                "is_required": is_mandatory,
            }))
            return

        # Verify checksum
        if not updater.verify_checksum(download_path):
            logger.error("Checksum verification failed")
            await ws.send_text(json.dumps({
                "type": "update-error",
                "message": "Checksum verification failed",
                "is_required": is_mandatory,
            }))
            # Clean up bad file
            try:
                Path(download_path).unlink()
            except Exception:
                pass
            return

        logger.info("Update downloaded and verified: %s", download_path)

        # Apply update
        success = await updater.apply_update(download_path)
        if success:
            logger.info("Update applied successfully, exiting...")
            # Notify before exit
            await ws.send_text(json.dumps({
                "type": "update-applied",
                "version": updater.latest_version,
                "is_required": is_mandatory,
                "message": "App will restart with new version",
            }))
            # Exit to allow restart with new version
            sys.exit(0)
        else:
            logger.error("Failed to apply update")
            await ws.send_text(json.dumps({
                "type": "update-error",
                "message": "Failed to apply update",
                "is_required": is_mandatory,
            }))

    except Exception as e:
        logger.error("Update process failed: %s", e)
        try:
            await ws.send_text(json.dumps({
                "type": "update-error",
                "message": str(e),
                "is_required": is_mandatory,
            }))
        except Exception:
            pass


@app.post("/task-progress")
async def task_progress(payload: dict):
    """
    Receive progress updates from queue workers and forward to WebSocket.
    Expects at least task_id. Defaults type to 'task-progress' if missing.
    """
    task_id = payload.get("task_id")
    if not task_id:
        return JSONResponse({"message": "task_id is required"},
                            status_code=400)

    message = {
        **payload,
        "type": payload.get("type", "task-progress"),
    }

    await _send_progress_to_task_clients(message)
    return JSONResponse({"message": "queued for delivery"})


@app.get("/")
async def root():
    """Simple health check endpoint"""
    db = SessionLocal()
    try:
        # Count active tasks and incomplete tasks
        active_count = len(active_tasks)

        incomplete_count = db.query(TaskLog).filter(
            (TaskLog.status == TaskStatus.RUNNING) |
            (TaskLog.status == TaskStatus.PENDING) &
            (TaskLog.sent_ack is False)
        ).count()

        logger.info(
            "Health check: %s active tasks, %s incomplete tasks",
            active_count,
            incomplete_count,
        )
        return JSONResponse({
            "message": "Agent is running",
            "active_tasks": active_count,
            "incomplete_tasks": incomplete_count,
            "registered_tasks": len(task_registry)
        })
    finally:
        db.close()
