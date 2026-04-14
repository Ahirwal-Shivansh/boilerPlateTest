from typing import Callable, Dict, Any, Optional
from eg_agent.log_config import logger as base_logger

# This is the central registry where all tasks are stored
task_registry: Dict[str, Dict[str, Any]] = {}
logger = base_logger.getChild("registry")


def register_task(name: str, for_queue: bool = False,
                  queue_name: str = "default"):
    """
    Decorator to register a task (sync or async) with queue support
    """
    def decorator(func: Callable[..., Any]):
        if not callable(func):
            raise ValueError("Task must be callable")

        if not name:
            raise ValueError("Task name cannot be empty")

        if name in task_registry:
            logger.warning("Task '%s' already registered", name)
            return func

        # Store task with metadata including queue info
        task_registry[name] = {
            'function': func,
            'for_queue': for_queue,
            'queue_name': queue_name,
            'is_async': (hasattr(func, '__code__')
                         and 'async' in str(func.__code__.co_flags))
        }
        logger.info(
            "Task registered: %s (queue: %s, for_queue: %s)",
            name, queue_name, for_queue
        )
        return func

    return decorator


def get_task(name: str) -> Optional[Dict[str, Any]]:
    """Get task with its metadata"""
    return task_registry.get(name)


# ---------------------------------------------------------------------------
# Built-in tasks (registered by default)
# ---------------------------------------------------------------------------

@register_task("ping")
async def _builtin_ping() -> str:
    """
    Health-check task that always returns 'pong'.

    This task is registered by default so users can verify their agent/server
    connectivity end-to-end before implementing custom tasks.
    """
    logger.debug("Built-in ping called")
    return "pong"
