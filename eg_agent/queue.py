import os
import sys
import importlib.util
import json
import asyncio
import sqlite3
import urllib.request
import urllib.error
from typing import Dict, Any, List, Tuple
from huey import SqliteHuey
from eg_agent.paths import get_app_path
from eg_agent.log_config import logger as base_logger
from eg_agent.db import (SessionLocal, update_task_status, TaskStatus,
                         get_task_by_id)
from eg_agent.registry import get_task as get_registry_task

logger = base_logger.getChild("queue")

queue_registry: Dict[str, SqliteHuey] = {}
task_functions: Dict[str, Any] = {}
_queues_discovered = False


def load_user_tasks_in_worker():
    """Load user tasks in worker process."""
    try:
        from eg_agent import loader
        loader.load_user_tasks()
        logger.info("✅ User tasks loaded in worker process")
    except Exception as e:
        logger.error(f"❌ Failed to load user tasks in worker: {e}")
        load_user_tasks_directly()


def load_user_tasks_directly():
    """Load tasks.py from current directory."""
    cwd = os.getcwd()
    tasks_file = os.path.join(cwd, "tasks.py")

    if os.path.exists(tasks_file):
        try:
            module_name = "user_tasks"
            spec = importlib.util.spec_from_file_location(module_name,
                                                          tasks_file)
            user_tasks = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = user_tasks
            spec.loader.exec_module(user_tasks)
            logger.info("✅ Directly loaded user tasks from tasks.py")
        except Exception as e:
            logger.error(f"❌ Failed to directly load tasks.py: {e}")
    else:
        logger.warning("⚠️ tasks.py not found in current directory")


def init_default_queues():
    """Initialize default queues with pre-registered task functions."""
    base_path = get_app_path('queues')
    os.makedirs(base_path, exist_ok=True)

    default_db_path = os.path.join(base_path, "default.db")
    default_queue = SqliteHuey('default', filename=default_db_path)
    queue_registry['default'] = default_queue

    @default_queue.task()
    def default_queue_task(task_data_str: str):
        load_user_tasks_in_worker()
        return execute_queued_task(task_data_str)

    task_functions['default'] = default_queue_task
    logger.info("Default queue initialized with task function")


def discover_custom_queues():
    """Dynamically discover and register custom queues."""
    global _queues_discovered

    if _queues_discovered:
        logger.info("Skipping queue discovery; already completed once")
        return

    cwd = os.getcwd()
    queue_files = []

    for root, dirs, files in os.walk(cwd):
        if "queues.py" in files:
            queue_files.append(os.path.join(root, "queues.py"))

    for queue_file in queue_files:
        try:
            module_name = os.path.splitext(os.path.basename(queue_file))[0]
            spec = importlib.util.spec_from_file_location(module_name,
                                                          queue_file)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if isinstance(attr, SqliteHuey):
                    queue_name = getattr(attr, 'name', attr_name)

                    if queue_name in queue_registry:
                        logger.info("Queue '%s' already registered; "
                                    "skipping duplicate", queue_name)
                        continue

                    @attr.task()
                    def custom_queue_task(task_data_str: str):
                        load_user_tasks_in_worker()
                        return execute_queued_task(task_data_str)

                    queue_registry[queue_name] = attr
                    task_functions[queue_name] = custom_queue_task
                    logger.info("Discovered and registered queue: %s",
                                queue_name)

        except Exception as e:
            logger.error("Error loading queue file %s: %s", queue_file, e)

    _queues_discovered = True


def get_queue(queue_name: str = "default") -> SqliteHuey:
    """Get queue instance by name; auto-create if missing."""
    if queue_name in queue_registry:
        return queue_registry[queue_name]

    logger.info("Queue '%s' not found; creating on-demand", queue_name)
    return create_queue(queue_name)


def get_task_function(queue_name: str = "default"):
    """Get the pre-registered task function for a queue."""
    if queue_name not in task_functions:
        get_queue(queue_name)
    return task_functions.get(queue_name, task_functions['default'])


def create_queue(queue_name: str, workers: int = 1) -> SqliteHuey:
    """Create a new queue with pre-registered task function."""
    if queue_name in queue_registry:
        logger.warning("Queue '%s' already exists", queue_name)
        return queue_registry[queue_name]

    base_path = get_app_path('queues')
    os.makedirs(base_path, exist_ok=True)

    db_path = os.path.join(base_path, f"{queue_name}.db")
    huey_instance = SqliteHuey(queue_name, filename=db_path)

    @huey_instance.task()
    def new_queue_task(task_data_str: str):
        load_user_tasks_in_worker()
        return execute_queued_task(task_data_str)

    queue_registry[queue_name] = huey_instance
    task_functions[queue_name] = new_queue_task

    logger.info("Created queue: %s with pre-registered task function",
                queue_name)
    return huey_instance


def execute_queued_task(task_data_str: str):
    """Universal task executor for all queued tasks."""
    try:
        task_data = json.loads(task_data_str)
        task_id = task_data['task_id']
        task_name = task_data['task_name']
        params = task_data['params']

        logger.info("[Huey Worker] Executing queued task_id=%s task_name=%s",
                    task_id, task_name)
        logger.info(f"🔄 Executing queued task: {task_name} (ID: {task_id})")

        db = SessionLocal()
        try:
            update_task_status(db, task_id, TaskStatus.RUNNING)
            logger.info(f"📊 Updated task {task_id} status to RUNNING")
        finally:
            db.close()

        task_info = get_registry_task(task_name)
        if not task_info:
            load_user_tasks_in_worker()
            task_info = get_registry_task(task_name)

            if not task_info:
                error_msg = (f"Task '{task_name}' not found in registry after "
                             f"reload. Available tasks: {list_all_tasks()}")
                logger.error(error_msg)
                raise ValueError(error_msg)

        task_func = task_info['function']
        logger.info(f"🎯 Found task function: {task_name}")

        # Prepare ws_send helper for queued tasks
        # This posts progress messages back to the server for WS forwarding
        def ws_send_queued(message: dict):
            """Post progress message to the agent server."""
            _post_progress_to_server(task_id, task_name, message)

        async def ws_send_queued_async(message: dict):
            """Async version - posts progress message to the agent server."""
            _post_progress_to_server(task_id, task_name, message)

        # Inject ws_send into params if the task accepts it
        import inspect
        sig = inspect.signature(task_func)
        if "ws_send" in sig.parameters:
            if asyncio.iscoroutinefunction(task_func):
                params = {**params, "ws_send": ws_send_queued_async}
            else:
                params = {**params, "ws_send": ws_send_queued}

        if asyncio.iscoroutinefunction(task_func):
            logger.info(f"⚡ Executing async task: {task_name}")
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(task_func(**params))
                logger.info(f"✅ Async task {task_name} completed "
                            f"successfully")

                db = SessionLocal()
                try:
                    update_task_status(db, task_id, TaskStatus.SUCCESS, result)
                    logger.info(f"📊 Updated task {task_id} status to SUCCESS")
                finally:
                    db.close()

                return result
            finally:
                loop.close()
        else:
            logger.info(f"⚡ Executing sync task: {task_name}")
            result = task_func(**params)
            logger.info(f"✅ Sync task {task_name} completed "
                        f"successfully")

            db = SessionLocal()
            try:
                update_task_status(db, task_id, TaskStatus.SUCCESS, result)
                logger.info(f"📊 Updated task {task_id} status to SUCCESS")
            finally:
                db.close()

            return result

    except Exception as e:
        logger.error(f"❌ Task execution failed: {str(e)}")

        db = SessionLocal()
        try:
            update_task_status(db, task_id, TaskStatus.ERROR, result=str(e))
            logger.info(f"📊 Updated task {task_id} status to ERROR")
        finally:
            db.close()

        raise


def _post_progress_to_server(task_id: str, task_name: str, message: dict):
    """
    Post a progress payload back to the FastAPI server so it can be
    forwarded to the relevant websocket client. Uses HTTP POST to
    <AGENT_SERVER_URL>/task-progress. Defaults to http://localhost:8080.
    """
    server_url = os.environ.get("AGENT_SERVER_URL", "http://localhost:8080")
    url = server_url.rstrip("/") + "/task-progress"

    payload = {
        **message,
        "task_id": message.get("task_id", task_id),
        "task_name": message.get("task_name", task_name),
    }
    if "type" not in payload:
        payload["type"] = "task-progress"

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except urllib.error.URLError as e:
        logger.warning(f"Failed to post progress to server: {e}")
    except Exception as e:
        logger.warning(f"Unexpected error posting progress to server: {e}")


def _get_next_priority_for_queue(huey_instance: SqliteHuey) -> int:
    """Return the priority for a newly enqueued task.

    In this agent we treat **larger numeric priority as higher priority**.
    When inserting a new task we:
    - Re-normalize priorities of existing pending tasks so they form a compact
      descending range (N, N-1, ..., 1) in current execution order.
    - Assign the new task priority N+1 so it becomes the next task to execute.
    """
    pending_tasks = _get_pending_tasks_from_huey_db(huey_instance)

    # No pending tasks -> first task simply gets priority 1.
    if not pending_tasks:
        return 1

    # pending_tasks is a list of (huey_task_id, priority, db_id) tuples,
    # ordered by priority ASC. If higher numbers run first, then the
    # current execution order is the reverse of this list.
    tasks_in_exec_order = list(reversed(pending_tasks))

    existing_count = len(tasks_in_exec_order)
    new_total_count = existing_count + 1

    # Reassign priorities for existing tasks so that, after we insert the
    # new task at the "top", priorities are:
    #   new_task: new_total_count
    #   current_top: new_total_count - 1
    #   ...
    #   current_bottom: 1
    for idx, (_huey_id, _old_priority, db_id) in enumerate(
            tasks_in_exec_order):
        # existing_count - idx gives N, N-1, ..., 1
        new_priority = existing_count - idx
        try:
            _update_task_priority_in_huey_db(
                huey_instance, db_id, new_priority)
        except Exception as e:
            logger.warning(
                "Failed to reassign priority for task db_id=%s in queue '%s': "
                "%s",
                db_id, huey_instance.name, e
            )

    # The new task should always be inserted at the highest numeric priority.
    return new_total_count


def list_all_tasks():
    """List all available tasks for debugging."""
    from eg_agent.registry import task_registry
    return list(task_registry.keys())


class TaskExecutor:
    """Handles task execution both immediate and queued."""

    @staticmethod
    def enqueue_task(task_name: str, task_func, params: Dict[str, Any],
                     task_id: str, queue_name: str = "default"):
        """Enqueue task using pre-registered task function."""
        task_function = get_task_function(queue_name)
        huey_instance = get_queue(queue_name)

        task_data = {
            'task_id': task_id,
            'task_name': task_name,
            'params': params,
            'queue_name': queue_name
        }
        task_data_str = json.dumps(task_data)

        next_priority = _get_next_priority_for_queue(huey_instance)
        huey_task = task_function(task_data_str, priority=next_priority)

        logger.info("✅ Task %s enqueued in queue '%s' with huey task id: %s",
                    task_id, queue_name, huey_task.id)
        return huey_task

    @staticmethod
    def dequeue_task(task_id: str, queue_name: str = None) -> Dict[str, Any]:
        """Dequeue (cancel) a task by task_id."""
        from eg_agent.db import (SessionLocal, get_task_by_id,
                                 update_task_status, TaskStatus)

        db = SessionLocal()
        try:
            task = get_task_by_id(db, task_id)
            if not task:
                return {
                    "success": False,
                    "message": f"Task {task_id} not found in database",
                    "task_id": task_id
                }

            if not task.for_queue:
                return {
                    "success": False,
                    "message": f"Task {task_id} is not a queued task",
                    "task_id": task_id,
                    "task_name": task.task_name
                }

            if task.status in [TaskStatus.SUCCESS, TaskStatus.ERROR]:
                return {
                    "success": False,
                    "message": (f"Task {task_id} is already completed "
                                f"(status: {task.status})"),
                    "task_id": task_id,
                    "task_name": task.task_name,
                    "status": task.status
                }

            actual_queue_name = queue_name or task.queue_name or "default"
            huey_instance = get_queue(actual_queue_name)

            if task.huey_task_id:
                try:
                    huey_instance.revoke_by_id(task.huey_task_id)
                    logger.info(f"✅ Revoked Huey task {task.huey_task_id} "
                                f"for task {task_id}")
                except Exception as e:
                    logger.warning(f"Failed to revoke Huey task "
                                   f"{task.huey_task_id}: {e}")

            update_task_status(db, task_id, "cancelled",
                               "Task dequeued by user")

            logger.info(f"✅ Task {task_id} ({task.task_name}) "
                        f"dequeued from queue '{actual_queue_name}'")

            return {
                "success": True,
                "message": f"Task {task_id} dequeued successfully",
                "task_id": task_id,
                "task_name": task.task_name,
                "queue_name": actual_queue_name,
                "huey_task_id": task.huey_task_id,
                "status": "cancelled"
            }

        except Exception as e:
            logger.error(f"❌ Failed to dequeue task {task_id}: {str(e)}")
            return {
                "success": False,
                "message": f"Failed to dequeue task: {str(e)}",
                "task_id": task_id
            }
        finally:
            db.close()


def _get_pending_tasks_from_huey_db(
        huey_instance: SqliteHuey) -> List[Tuple[str, int, str]]:
    """Fetch all pending tasks from Huey's SQLite database ordered by priority.

    Returns list of (huey_task_id, priority, db_id) tuples ordered by
    priority ascending.
    """
    import pickle
    db_path = str(get_app_path(f'queues/{huey_instance.name}.db'))
    if not db_path:
        return []

    queue_name = huey_instance.name
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, data, priority FROM task WHERE queue=? "
            "ORDER BY priority ASC",
            (queue_name,))
        rows = cursor.fetchall()

        result = []
        for db_id, data_blob, priority in rows:
            try:
                data = pickle.loads(data_blob)
                huey_task_id = data.id
                result.append((huey_task_id, priority, db_id))
            except Exception as e:
                logger.warning(
                    f"Failed to unpickle task data for db_id {db_id}: {e}")
                continue

        return result
    except sqlite3.OperationalError as e:
        logger.error(f"Error querying Huey database: {e}")
        return []
    finally:
        conn.close()


def _update_task_priority_in_huey_db(
        huey_instance: SqliteHuey,
        db_id: str,
        new_priority: int) -> bool:
    """Update task priority using database internal id.

    Args:
        huey_instance: The Huey queue instance
        db_id: The internal database row id (not huey_task_id)
        new_priority: The new priority value
    """
    db_path = huey_instance.storage_kwargs.get('filename')
    if not db_path:
        return False

    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE task
            SET priority = ?
            WHERE id = ?
        """, (new_priority, db_id))
        conn.commit()
        return cursor.rowcount > 0
    except sqlite3.OperationalError:
        # Try alternative table name
        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]
            for table in tables:
                try:
                    cursor.execute(f"""
                        UPDATE {table}
                        SET priority = ?
                        WHERE id = ?
                    """, (new_priority, db_id))
                    conn.commit()
                    if cursor.rowcount > 0:
                        return True
                except sqlite3.OperationalError:
                    continue
        except Exception as e:
            logger.error(f"Error updating priority in Huey database: {e}")
        return False
    finally:
        conn.close()


def reorder_task_in_queue(queue_name: str, custom_task_id: str,
                          new_position: int) -> Dict[str, Any]:
    """Reorder a task in a Huey SQLite queue.

    Args:
        queue_name: Name of the queue
        custom_task_id: Custom task ID (from our database)
        new_position: New 1-based position in the queue

    Returns:
        Dict with success status and message
    """
    from eg_agent.db import SessionLocal

    # Get huey_task_id from custom_task_id
    db = SessionLocal()
    try:
        task = get_task_by_id(db, custom_task_id)
        if not task:
            return {
                "success": False,
                "message": f"Task {custom_task_id} not found in database",
                "task_id": custom_task_id
            }

        if not task.huey_task_id:
            return {
                "success": False,
                "message": f"Task {custom_task_id} has no huey_task_id",
                "task_id": custom_task_id
            }

        huey_task_id = task.huey_task_id
    finally:
        db.close()

    # Get queue instance and its current pending tasks
    huey_instance = get_queue(queue_name)
    pending_tasks = _get_pending_tasks_from_huey_db(huey_instance)

    if not pending_tasks:
        return {
            "success": False,
            "message": f"No pending tasks found in queue '{queue_name}'",
            "task_id": custom_task_id
        }

    # pending_tasks: list of (huey_task_id, priority, db_id), ordered by
    # priority ASC. If higher numbers run first, then the *execution* order
    # is the reverse of this list.
    tasks_in_exec_order = list(reversed(pending_tasks))

    target_index = None
    for idx, (huey_id, _priority, _db_id) in enumerate(tasks_in_exec_order):
        if huey_id == huey_task_id:
            target_index = idx
            break

    if target_index is None:
        logger.warning(
            "Task %s (huey_id: %s) not found among pending tasks in "
            "queue '%s'. It may have already run or been removed.",
            custom_task_id, huey_task_id, queue_name
        )
        return {
            "success": False,
            "message": (
                f"Task {custom_task_id} (huey_id: {huey_task_id}) not found "
                f"in pending tasks for queue '{queue_name}'. "
                f"It may have already been executed or removed."
            ),
            "task_id": custom_task_id,
            "huey_task_id": huey_task_id
        }

    # Remove the target task from its current position
    task_to_move = tasks_in_exec_order.pop(target_index)

    # Validate new_position (1-based)
    if new_position < 1:
        new_position = 1
    elif new_position > len(tasks_in_exec_order) + 1:
        new_position = len(tasks_in_exec_order) + 1

    # Insert at new position (convert to 0-based index)
    tasks_in_exec_order.insert(new_position - 1, task_to_move)

    # Update priorities for all tasks sequentially so that:
    # - Position 1 has the **highest** numeric priority (executes first)
    # - Position 2 has the second-highest, etc.
    total_tasks = len(tasks_in_exec_order)
    for idx, (_huey_id, _old_priority, db_id) in enumerate(
            tasks_in_exec_order):
        # Example: for 3 tasks, priorities will be 3, 2, 1
        new_priority = total_tasks - idx
        _update_task_priority_in_huey_db(huey_instance, db_id, new_priority)

    logger.info(
        "✅ Reordered task %s to position %d in queue '%s'",
        custom_task_id, new_position, queue_name
    )

    return {
        "success": True,
        "message": (
            f"Task {custom_task_id} reordered to position {new_position} "
            f"in queue '{queue_name}'"),
        "task_id": custom_task_id,
        "huey_task_id": huey_task_id,
        "new_position": new_position,
        "queue_name": queue_name
    }


init_default_queues()
logger.info("🎯 Queue system initialized. Available queues: %s",
            list(queue_registry.keys()))
