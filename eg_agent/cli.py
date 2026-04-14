import click
import uvicorn
import sys
import threading
import os
import sqlite3
from importlib import resources
from pathlib import Path
from eg_agent.log_config import logger as base_logger
from eg_agent.paths import get_app_path
from eg_agent.loader import load_dotenv_file

logger = base_logger.getChild("cli")

# Load environment variables from .env file
load_dotenv_file()


@click.group()
def main():
    """EG-Agent CLI - Run agent server and tools."""
    pass


def start_worker(queue_name: str):
    """Start a Huey worker in a separate process."""
    try:
        logger.info("Starting worker for queue '%s'", queue_name)

        from eg_agent import queue, loader

        try:
            queue.discover_custom_queues()
        except Exception as e:
            logger.warning("Queue discovery in worker failed: %s", e)

        try:
            loader.load_user_tasks()
            logger.info("Loaded user tasks in %s worker", queue_name)
        except Exception as e:
            logger.warning("Failed to load tasks via loader: %s", e)
            queue.load_user_tasks_directly()

        from huey.consumer import Consumer
        huey_instance = queue.get_queue(queue_name)
        worker = Consumer(huey_instance)
        worker.loglevel = 'INFO'
        worker.workers = 1

        logger.info("Worker for queue '%s' started successfully", queue_name)
        worker.run()
    except Exception as e:
        logger.error("Worker for queue '%s' failed: %s", queue_name, e)
        raise


@main.command()
@click.option("--host",
              default=lambda: os.getenv("HOST", "127.0.0.1"),
              help="Host to bind.")
@click.option("--port",
              default=lambda: int(os.getenv("PORT", "8080")),
              type=int,
              help="Port to bind.")
@click.option("--tray/--no-tray", default=True, help="Run with tray icon")
def run(host, port, tray):
    """Run the agent server with queue workers."""
    from eg_agent.server import app
    from eg_agent.tray import start_tray_icon

    def _run_server():
        logger.info("Starting uvicorn server on %s:%s", host, port)
        uvicorn.run(app, host=host, port=port, log_level="info")

    if tray:
        logger.info("Tray enabled")
        if sys.platform == "darwin":
            server_thread = threading.Thread(target=_run_server, daemon=True)
            server_thread.start()
            logger.info("Server thread started; launching tray on main thread")
            start_tray_icon(blocking=True)
        else:
            start_tray_icon(blocking=False)
            _run_server()
    else:
        _run_server()


@main.command()
@click.option("--host",
              default=lambda: os.getenv("HOST", "127.0.0.1"),
              help="Host to bind.")
@click.option("--port",
              default=lambda: int(os.getenv("PORT", "8080")),
              type=int,
              help="Port to bind.")
@click.option("--tray/--no-tray", default=True, help="Run with tray icon")
@click.option("--workers-config",
              default=lambda: os.getenv("WORKERS_CONFIG", "workers.json"),
              help="Path to workers config file (default: workers.json)")
def serve(host, port, tray, workers_config):
    """
    Start the agent server and configured queue workers.

    This command:
    1. Initializes the database if needed
    2. Starts the agent server
    3. Starts queue workers as configured in workers.json

    Workers are started in separate processes and will automatically restart
    if they crash. The server runs in the foreground (or with tray icon).
    """
    import json
    import subprocess
    from pathlib import Path

    # Initialize database first
    from eg_agent.db import init_db
    logger.info("Initializing database...")
    init_db()

    # Load workers configuration
    workers_file = Path(workers_config)
    workers_to_start = []

    if workers_file.exists():
        try:
            with open(workers_file, 'r') as f:
                config = json.load(f)
                workers_to_start = config.get('workers', [])
                logger.info(
                    "Loaded workers configuration from %s", workers_config)
        except Exception as e:
            logger.warning(
                "Failed to load workers config from %s: %s",
                workers_config, e)
    else:
        logger.info(
            "No workers config at %s, starting default queue",
            workers_config)
        # Default: start the default queue if no config
        workers_to_start = [{"queue": "default", "workers": 1}]

    # Start workers in separate processes
    worker_processes = []
    for worker_cfg in workers_to_start:
        queue_name = worker_cfg.get('queue', 'default')
        num_workers = worker_cfg.get('workers', 1)

        logger.info(
            "Starting %d worker(s) for queue '%s'", num_workers, queue_name)
        try:
            if getattr(sys, 'frozen', False):
                agent_exe = Path(sys.executable)
                if sys.platform == "win32":
                    worker_exe = agent_exe.parent / "eg-agent-worker.exe"
                else:
                    worker_exe = agent_exe.parent / "eg-agent-worker"
                if worker_exe.exists():
                    worker_cmd = [
                        str(worker_exe), queue_name,
                        '--workers', str(num_workers)]
                else:
                    worker_cmd = [
                        'eg-agent-worker', queue_name,
                        '--workers', str(num_workers)]
            else:
                worker_cmd = [
                    sys.executable, "-m", "eg_agent.worker",
                    queue_name, "--workers", str(num_workers)]

            # Start worker process
            proc = subprocess.Popen(
                worker_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            worker_processes.append((queue_name, proc))
            logger.info(
                "Started worker process for queue '%s' (PID: %d)",
                queue_name, proc.pid)
        except Exception as e:
            logger.error(
                "Failed to start worker for queue '%s': %s", queue_name, e)

    # Now start the server (this will block)
    from eg_agent.server import app
    from eg_agent.tray import start_tray_icon

    def _run_server():
        logger.info("Starting uvicorn server on %s:%s", host, port)
        uvicorn.run(app, host=host, port=port, log_level="info")

    if tray:
        logger.info("Tray enabled")
        if sys.platform == "darwin":
            server_thread = threading.Thread(target=_run_server, daemon=True)
            server_thread.start()
            logger.info("Server thread started; launching tray on main thread")
            start_tray_icon(blocking=True)
        else:
            start_tray_icon(blocking=False)
            _run_server()
    else:
        _run_server()


@main.command()
@click.argument("queue_name")
@click.option("--workers",
              default=lambda: int(os.getenv("WORKERS", "1")),
              type=int,
              help="Number of workers for this queue")
def create_queue_cmd(queue_name, workers):
    """Create a new queue with SQLite backend."""
    from eg_agent.queue import queue_registry, create_queue

    if queue_name in queue_registry:
        click.echo(f"❌ Queue '{queue_name}' already exists!")
        return

    create_queue(queue_name, workers)
    click.echo(f"✅ Queue '{queue_name}' created successfully!")
    click.echo(f"   Workers: {workers}")


@main.command()
def list_queues():
    """List all available queues."""
    from eg_agent.queue import queue_registry

    if not queue_registry:
        click.echo("❌ No queues found!")
        return

    click.echo("📊 Available Queues:")
    for name, huey_instance in queue_registry.items():
        db_filename = huey_instance.storage_kwargs.get('filename', 'N/A')
        click.echo(f"   • {name} (DB: {os.path.basename(db_filename)})")


@main.command()
def db_path():
    """Get the path to the database."""
    db_filename = os.getenv("DB_FILENAME", "eg_agent.db")
    db_path = get_app_path(db_filename)
    logger.info("Database path: %s", str(db_path))
    click.echo(f"Database path: {str(db_path)}")


@main.command()
def log_path():
    """Get the path to the log file."""
    log_filename = os.getenv("LOG_FILENAME", "eg_agent.log")
    log_path = get_app_path(log_filename)
    logger.info("Log path: %s", str(log_path))
    click.echo(f"Log path: {str(log_path)}")


@main.command()
def init_db():
    """Initialize the database."""
    from eg_agent.db import init_db as init_db_func

    init_db_func()
    db_filename = os.getenv("DB_FILENAME", "eg_agent.db")
    db_path = get_app_path(db_filename)
    logger.info("Database initialized via CLI command")
    click.echo(f"Database initialized at: {str(db_path)}")


@main.command()
@click.option("--migrate", "migrate_flag", is_flag=True, default=False,
              help="Alias for migrate-db command")
def migrate_db(migrate_flag):
    """Migrate database schema (adds new columns if needed)."""
    from eg_agent.db import init_db

    db_filename = os.getenv("DB_FILENAME", "eg_agent.db")
    db_path = get_app_path(db_filename)
    init_db()

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute('PRAGMA table_info(agent_tasks)')
        columns = [column[1] for column in cursor.fetchall()]

        if 'huey_task_id' not in columns:
            click.echo("Adding huey_task_id column...")
            cursor.execute('ALTER TABLE agent_tasks ADD COLUMN huey_task_id '
                           'VARCHAR(255)')
            conn.commit()
            click.echo("✅ huey_task_id column added")
        else:
            click.echo("✅ huey_task_id column already exists")

        conn.close()

    except Exception as e:
        click.echo(f"⚠️ Manual migration warning: {e}")

    logger.info("Database migration completed via CLI command")
    click.echo(f"Database migrated at: {str(db_path)}")
    click.echo("✅ Migration completed - all fields added if needed")


@main.command("migrate")
def migrate_alias():
    """Alias for migrate-db command."""
    ctx = click.get_current_context()
    ctx.invoke(migrate_db, migrate_flag=True)


@main.command()
@click.argument("task_id")
@click.option("--queue", "queue_name", help="Queue name (optional)")
def dequeue_task(task_id, queue_name):
    """Dequeue (cancel) a task by task_id."""
    from eg_agent.queue import TaskExecutor

    result = TaskExecutor.dequeue_task(task_id, queue_name)

    if result["success"]:
        click.echo(f"✅ Task {task_id} dequeued successfully!")
        click.echo(f"   Task: {result.get('task_name', 'Unknown')}")
        click.echo(f"   Queue: {result.get('queue_name', 'Unknown')}")
        click.echo(f"   Status: {result.get('status', 'Unknown')}")
        if result.get('huey_task_id'):
            click.echo(f"   Huey Task ID: {result['huey_task_id']}")
    else:
        click.echo(f"❌ Failed to dequeue task {task_id}")
        click.echo(f"   Error: {result['message']}")
        sys.exit(1)


@main.command("scaffold-windows")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite existing files if present")
def scaffold_windows(force):
    """Copy Windows packaging templates into current directory."""
    target_files = {
        "build_windows.bat": ("eg_agent", "templates", "build_windows.bat"),
        "eg_agent_standalone.py": ("eg_agent", "templates",
                                   "eg_agent_standalone.py"),
    }

    cwd = os.getcwd()
    created_or_overwritten = []
    skipped = []

    for out_name, rel_parts in target_files.items():
        out_path = os.path.join(cwd, out_name)
        if os.path.exists(out_path) and not force:
            skipped.append(out_name)
            continue

        package = rel_parts[0]
        resource_subpath = os.path.join(*rel_parts[1:])
        try:
            resource_file = resources.files(package).joinpath(resource_subpath)
            with resource_file.open("rb") as src:
                data = src.read()
            with open(out_path, "wb") as dst:
                dst.write(data)
            created_or_overwritten.append(out_name)
        except Exception as exc:
            logger.exception("Failed to materialize template %s: %s",
                             out_name, exc)
            raise click.ClickException(f"Failed to write {out_name}: {exc}")

    if created_or_overwritten:
        click.echo("Scaffolded: " + ", ".join(created_or_overwritten))
    if skipped:
        message = "Skipped (exists): " + ", ".join(skipped)
        message += ". Use --force to overwrite."
        click.echo(message)


@main.command("scaffold-macos")
@click.option("--force", is_flag=True, default=False,
              help="Overwrite existing files if present")
def scaffold_macos(force):
    """Copy macOS packaging template and make it executable."""
    filename = "build_macos.sh"
    cwd = os.getcwd()
    out_path = os.path.join(cwd, filename)

    if os.path.exists(out_path) and not force:
        click.echo("Skipped (exists): build_macos.sh. "
                   "Use --force to overwrite.")
        return

    try:
        resource_file = resources.files("eg_agent").joinpath(
            os.path.join("templates", filename))
        with resource_file.open("rb") as src:
            data = src.read()
        with open(out_path, "wb") as dst:
            dst.write(data)
        try:
            import stat
            current_mode = os.stat(out_path).st_mode
            os.chmod(out_path, current_mode | stat.S_IXUSR |
                     stat.S_IXGRP | stat.S_IXOTH)
        except Exception:
            pass
    except Exception as exc:
        logger.exception("Failed to materialize template %s: %s",
                         filename, exc)
        raise click.ClickException(f"Failed to write {filename}: {exc}")

    click.echo("Scaffolded: build_macos.sh")


@main.command("scaffold-packaging")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite existing files/directories if present",
)
def scaffold_packaging(force: bool) -> None:
    """
    Copy packaging code into the current working directory.

    This command scaffolds everything needed to build MSI/PKG installers:
      - installer_helper/        (Helper binary source)
      - installer_hooks.py       (Simple hooks - customize this!)
      - build_windows_installer.py
      - build_macos_installer.py

    After scaffolding, customize installer_hooks.py with your pre/post
    install logic, then run:
      python build_windows_installer.py  (Windows)
      python build_macos_installer.py    (macOS)
    """

    import shutil

    cwd = Path.cwd()

    created_or_overwritten: list[str] = []
    skipped: list[str] = []

    def _copy_package_tree(package_name: str, target_name: str) -> None:
        src_root = resources.files(package_name)
        target_root = cwd / target_name

        if target_root.exists() and not force:
            skipped.append(f"{target_name}/")
            return

        if target_root.exists() and force:
            shutil.rmtree(target_root, ignore_errors=True)

        for item in src_root.rglob("*"):
            rel = item.relative_to(src_root)
            dest = target_root / rel

            if item.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with item.open("rb") as src_f:
                    data = src_f.read()
                with open(dest, "wb") as dst_f:
                    dst_f.write(data)
        created_or_overwritten.append(f"{target_name}/")

    def _copy_file(src_package: str, src_file: str, dest_name: str) -> None:
        dest_path = cwd / dest_name
        if dest_path.exists() and not force:
            skipped.append(dest_name)
            return
        try:
            src = resources.files(src_package).joinpath(src_file)
            with src.open("rb") as f:
                data = f.read()
            with open(dest_path, "wb") as f:
                f.write(data)
            created_or_overwritten.append(dest_name)
        except Exception as exc:
            raise click.ClickException(
                f"Failed to scaffold {dest_name}: {exc}")

    # 1) Copy installer_helper package
    try:
        _copy_package_tree("installer_helper", "installer_helper")
    except Exception as exc:
        logger.exception("Failed to scaffold installer_helper: %s", exc)
        raise click.ClickException(
            f"Failed to scaffold installer_helper: {exc}")

    # 2) Copy installer_hooks.py template
    hooks_path = cwd / "installer_hooks.py"
    if hooks_path.exists() and not force:
        skipped.append("installer_hooks.py")
    else:
        hooks_content = '''"""
Installer Hooks - Simple functions called during installation.

Define the functions you need - all are optional. The installer helper
will call these during MSI/PKG installation.
"""


def pre_install(install_root: str, data_root: str, is_upgrade: bool) -> None:
    """
    Called BEFORE database migrations.

    Use for: backups, validation, cleanup.
    """
    print(f"[pre_install] install_root={install_root}, upgrade={is_upgrade}")
    # Add your pre-install logic here


def post_install(install_root: str, data_root: str, is_upgrade: bool) -> None:
    """
    Called AFTER database migrations, before agent starts.

    Use for: data migration, config setup, scheduled tasks.
    """
    print(f"[post_install] install_root={install_root}, upgrade={is_upgrade}")
    # Add your post-install logic here
'''
        with open(hooks_path, "w", encoding="utf-8") as f:
            f.write(hooks_content)
        created_or_overwritten.append("installer_hooks.py")

    # 3) Copy build scripts
    _copy_file("eg_agent", "build_windows_installer.py",
               "build_windows_installer.py")
    _copy_file("eg_agent", "build_macos_installer.py",
               "build_macos_installer.py")

    # Report summary
    if created_or_overwritten:
        click.echo("Scaffolded: " + ", ".join(created_or_overwritten))
    if skipped:
        msg = "Skipped (exists): " + ", ".join(skipped)
        msg += ". Use --force to overwrite."
        click.echo(msg)

    click.echo("")
    click.echo("Next steps:")
    click.echo("  1. Edit installer_hooks.py with your install logic")
    click.echo("  2. Run: python build_windows_installer.py  (Windows)")
    click.echo("     Or:  python build_macos_installer.py    (macOS)")
