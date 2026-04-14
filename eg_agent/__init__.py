from importlib.metadata import version, PackageNotFoundError
try:
    __version__ = version("eg-agent")
except PackageNotFoundError:
    __version__ = "1.0.0"

# Configure logging when package is imported by worker
from eg_agent import log_config  # noqa: F401
