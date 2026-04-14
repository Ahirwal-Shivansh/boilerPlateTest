import importlib.util
import os
import sys
from pathlib import Path
from eg_agent.log_config import logger as base_logger

logger = base_logger.getChild("loader")

ENV_PRELOAD_FILES_KEYS = (
    "EG_AGENT_PRELOAD_FILES",
    "EG_AGENT_PRELOAD_MODULES",  # Back-compat
)


def _is_frozen() -> bool:
    """Check if running as a PyInstaller frozen executable."""
    return getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')


def _get_data_root() -> str:
    """Get platform-specific data root directory."""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "EG-Agent")
    elif sys.platform == "darwin":
        home = os.path.expanduser("~")
        return os.path.join(home, "Library", "Application Support", "EG-Agent")
    else:
        home = os.path.expanduser("~")
        return os.path.join(home, ".local", "share", "eg-agent")


def _find_tasks_file() -> tuple[str, str]:
    """Find tasks.py file, checking multiple locations."""
    search_paths = []

    if _is_frozen():
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            search_paths.append((meipass, meipass))
        data_root = _get_data_root()
        exe_dir = str(Path(sys.executable).parent.resolve())
        search_paths.extend([
            (data_root, data_root),
            (exe_dir, exe_dir),
        ])

    cwd = os.getcwd()
    search_paths.append((cwd, cwd))

    # Check EG_AGENT_TASKS_DIR env var
    tasks_dir = os.environ.get("EG_AGENT_TASKS_DIR")
    if tasks_dir:
        tasks_dir = os.path.expanduser(tasks_dir)
        if os.path.isabs(tasks_dir):
            search_paths.insert(0, (tasks_dir, cwd))
        else:
            search_paths.insert(0, (os.path.join(cwd, tasks_dir), cwd))

    for search_dir, cwd_to_use in search_paths:
        task_file = os.path.join(search_dir, "tasks.py")
        if os.path.exists(task_file):
            logger.info("Found tasks.py at: %s", task_file)
            return task_file, cwd_to_use

    logger.warning("No tasks.py found in: %s", [p[0] for p in search_paths])
    return None, None


def load_dotenv_file(directory: str = None) -> bool:
    """
    Load .env file from the specified directory or auto-detect location.

    For frozen executables, checks _MEIPASS first, then the specified directory.
    Falls back to manual parsing if python-dotenv is not available.

    Args:
        directory: Directory containing .env file. If None, auto-detects based
                   on frozen state (uses _MEIPASS for frozen, cwd otherwise).

    Returns:
        True if .env was loaded successfully, False otherwise.
    """
    search_paths = []

    if _is_frozen():
        meipass = getattr(sys, '_MEIPASS', '')
        if meipass:
            search_paths.append(meipass)

    if directory:
        search_paths.append(directory)
    else:
        search_paths.append(os.getcwd())

    dotenv_path = None
    for path in search_paths:
        candidate = os.path.join(path, ".env")
        if os.path.exists(candidate):
            dotenv_path = candidate
            break

    if not dotenv_path:
        logger.debug("No .env file found in: %s", search_paths)
        return False

    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=dotenv_path, override=False)
        logger.info("Loaded .env from: %s", dotenv_path)
        return True
    except ImportError:
        return _parse_dotenv_manually(dotenv_path)
    except Exception as e:
        logger.warning("Failed to load .env with dotenv: %s", e)
        return _parse_dotenv_manually(dotenv_path)


def _parse_dotenv_manually(dotenv_path: str) -> bool:
    """Parse .env file manually when python-dotenv is not available."""
    try:
        with open(dotenv_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, value = line.partition('=')
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        logger.info("Loaded .env (manual parse) from: %s", dotenv_path)
        return True
    except Exception as e:
        logger.warning("Failed to parse .env manually: %s", e)
        return False


def _load_cwd_dotenv(cwd: str) -> None:
    """Load .env file from directory. Deprecated: use load_dotenv_file()."""
    load_dotenv_file(cwd)


def _split_list_env(value: str) -> list:
    """Parse comma/semicolon/newline separated list."""
    parts = []
    for raw in value.replace(";", ",").replace("\n", ",").split(","):
        item = raw.strip().strip('"').strip("'")
        if item:
            parts.append(item)
    return parts


def _get_preload_targets() -> list:
    """Get preload targets from environment variables."""
    for key in ENV_PRELOAD_FILES_KEYS:
        value = os.environ.get(key)
        if value and value.strip():
            return _split_list_env(value)
    return []


def _preload_module(cwd: str, filename: str) -> None:
    """Preload a single module file."""
    module_path = os.path.join(cwd, filename)
    if not os.path.exists(module_path):
        return

    module_name = os.path.splitext(filename)[0]
    if module_name in sys.modules:
        return

    try:
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            logger.info("Preloaded: %s", filename)
    except Exception:
        logger.exception("Failed to preload: %s", filename)


def _preload_dependencies(tasks_dir: str) -> None:
    """Preload dependencies before loading tasks.py."""
    targets = _get_preload_targets()

    if targets:
        for entry in targets:
            filename = entry if entry.endswith(".py") else f"{entry}.py"
            _preload_module(tasks_dir, filename)
    else:
        try:
            for filename in os.listdir(tasks_dir):
                is_py = filename.endswith(".py")
                is_tasks = filename == "tasks.py"
                is_db = "db" in filename.lower()
                if is_py and not is_tasks and is_db:
                    _preload_module(tasks_dir, filename)
        except Exception:
            pass


def load_user_tasks():
    """Load tasks.py from bundle or filesystem."""
    task_file, _ = _find_tasks_file()
    if not task_file:
        logger.warning("No tasks.py found")
        return

    tasks_dir = os.path.dirname(task_file)

    if tasks_dir not in sys.path:
        sys.path.insert(0, tasks_dir)

    _load_cwd_dotenv(tasks_dir)
    _preload_dependencies(tasks_dir)

    module_name = f"user_tasks_{hash(task_file) % 10000}"
    spec = importlib.util.spec_from_file_location(module_name, task_file)
    if not spec or not spec.loader:
        logger.error("Could not create import spec for: %s", task_file)
        return

    user_tasks = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = user_tasks

    try:
        spec.loader.exec_module(user_tasks)
        logger.info("Loaded tasks from: %s", task_file)
    except Exception as e:
        logger.exception("Failed to load tasks.py: %s", e)
