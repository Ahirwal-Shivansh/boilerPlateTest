"""
Packaged Huey worker entry point.

Usage:
    eg-agent-worker <queue_name> [-w WORKERS]
"""

import sys
import click
from huey.consumer import Consumer
from eg_agent.queue import get_queue, load_user_tasks_in_worker


@click.command()
@click.argument("queue_name")
@click.option("--workers", "workers", default=1, help="Number of workers")
def main(queue_name: str, workers: int):
    # Ensure user tasks imported in the worker
    try:
        load_user_tasks_in_worker()
    except Exception:
        pass

    huey_instance = get_queue(queue_name)
    consumer = Consumer(huey_instance, workers=int(workers))
    consumer.loglevel = 'INFO'
    consumer.run()


if __name__ == "__main__":
    # Fallback for direct python -m execution
    sys.exit(main())
