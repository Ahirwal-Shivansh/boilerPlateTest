import logging
import os
from eg_agent.paths import APP_NAME, get_app_path

# Get log filename from environment or use default
log_filename = os.getenv("LOG_FILENAME", "eg_agent.log")
log_file = get_app_path(log_filename)

# Configure logging only once
_logging_configured = False


def configure_logging():
    """Configure logging if not already configured."""
    global _logging_configured
    if not _logging_configured:
        root_logger = logging.getLogger()
        if not root_logger.handlers:
            file_handler = None
            try:
                file_handler = logging.FileHandler(log_file, encoding="utf-8")
            except PermissionError:
                # If the primary directory is root-owned, continue with stdout/stderr.
                # (We still keep logger creation working so the app can start.)
                file_handler = None
            logging.basicConfig(
                level=logging.INFO,  # change to DEBUG for more detail
                format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                handlers=[
                    file_handler if file_handler is not None else logging.StreamHandler(),
                    logging.StreamHandler()
                ]
            )
        _logging_configured = True
        logger = logging.getLogger(APP_NAME)
        logger.info("✅ Logging configured successfully")
        logger.info(f"Logs will be stored at: {log_file}")


# Configure logging on module import
configure_logging()

# Module-level logger
logger = logging.getLogger(APP_NAME)
