# eg_agent_standalone.py
import sys
import os
import time
import threading
import json
import subprocess
from pathlib import Path


def _load_dotenv_from_bundle() -> None:
    """Load .env file from frozen bundle or current directory."""
    try:
        from eg_agent.loader import load_dotenv_file
        load_dotenv_file()
    except ImportError:
        # Fallback for standalone execution before eg_agent is in path
        if getattr(sys, 'frozen', False):
            base_path = getattr(sys, '_MEIPASS', '')
        else:
            base_path = os.path.dirname(os.path.abspath(__file__))

        env_path = os.path.join(base_path, '.env')
        if os.path.exists(env_path):
            try:
                from dotenv import load_dotenv
                load_dotenv(env_path, override=False)
                print(f"Loaded .env from: {env_path}")
            except ImportError:
                _parse_env_manually(env_path)
            except Exception as e:
                print(f"Failed to load .env: {e}")


def _parse_env_manually(env_path: str) -> None:
    """Parse .env file manually when python-dotenv is not available."""
    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, value = line.partition('=')
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        print(f"Loaded .env (manual parse) from: {env_path}")
    except Exception as e:
        print(f"Failed to parse .env: {e}")


def setup_environment() -> None:
    """Set up the environment for running as executable"""
    if getattr(sys, 'frozen', False):
        # Running as compiled executable
        base_path = sys._MEIPASS
    else:
        # Running as script
        base_path = os.path.dirname(os.path.abspath(__file__))

    # Add site-packages to path
    site_packages = os.path.join(base_path, 'site-packages')
    if os.path.exists(site_packages):
        sys.path.insert(0, site_packages)

    # Add current directory to path
    sys.path.insert(0, base_path)

    # Load .env file from bundle
    _load_dotenv_from_bundle()


def _load_workers_config(install_root: Path) -> list[dict]:
    """
    Load workers configuration from workers.json next to the executable.

    Falls back to a single default queue worker if no config is found.
    """

    config_path = install_root / "workers.json"
    if config_path.exists():
        try:
            with config_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            workers = data.get("workers", [])
            if workers:
                print(f"Loaded workers configuration from {config_path}")
                return workers
        except Exception as exc:
            print(f"Failed to load workers.json: {exc}; using default")

    # Default: one worker for the "default" queue
    return [{"queue": "default", "workers": 1}]


def _start_workers(install_root: Path, workers: list[dict]) -> None:
    """
    Start worker processes for each configured queue.

    Looks for eg-agent-worker(.exe) next to the agent executable.
    """

    if sys.platform == "win32":
        worker_exe = install_root / "eg-agent-worker.exe"
    else:
        worker_exe = install_root / "eg-agent-worker"

    if not worker_exe.exists():
        print(f"Worker executable not found at {worker_exe}; "
              "workers will not start")
        return

    started_queues: list[str] = []

    for worker_cfg in workers:
        queue_name = worker_cfg.get("queue", "default")
        num_workers = worker_cfg.get("workers", 1)

        cmd = [
            str(worker_exe),
            queue_name,
            "--workers",
            str(num_workers),
        ]
        print(f"[eg-agent] Starting worker for queue '{queue_name}' "
              f"with {num_workers} worker(s): {cmd}")
        try:
            subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            started_queues.append(queue_name)
        except Exception as exc:
            print(f"Failed to start worker for queue {queue_name}: {exc}")

    if started_queues:
        print("[eg-agent] Workers started for queues: "
              f"{', '.join(started_queues)}")


def import_and_run_app() -> bool:
    """Import the app and run it directly with workers."""
    try:
        from eg_agent.server import app
        from eg_agent.tray import start_tray_icon
        from eg_agent.db import init_db
        import uvicorn

        print("Found app in tasks.py")

        # Initialize DB (idempotent)
        print("Initializing database...")
        init_db()

        # Determine install root (directory where this exe lives)
        install_root = Path(sys.executable).resolve().parent

        # Load worker configuration and start workers
        workers = _load_workers_config(install_root)
        _start_workers(install_root, workers)

        # Get host and port from environment
        host = os.environ.get("HOST", "127.0.0.1")
        port = int(os.environ.get("PORT", "8080"))

        print(f"Starting uvicorn server on {host}:{port}...")

        def _run_server() -> None:
            uvicorn.run(app, host=host, port=port, log_level="info")

        if sys.platform == "darwin":
            # macOS: run server in background, start tray on main thread
            server_thread = threading.Thread(target=_run_server, daemon=True)
            server_thread.start()
            start_tray_icon(blocking=True)
        else:
            # Other platforms: tray in background, server on main thread
            start_tray_icon(blocking=False)
            _run_server()
        return True

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return False


def main() -> None:
    """Main function"""
    print("=" * 50)
    print("        eg-agent Server")
    print("=" * 50)

    # Setup environment
    setup_environment()

    # Run the server
    if not import_and_run_app():
        print("Failed to start server")
        time.sleep(5)

    input("Press Enter to exit...")


if __name__ == "__main__":
    main()
