class AgentCallsConstants:
    ACK_SENT_CALL = "ack-sent"
    ACK_RECEIVED_CALL = "ack-received"
    TASK_ERROR_CALL = "task-error"


class ActivationConstants:
    """Constants for activation key WebSocket messages."""
    ACTIVATION_REQUIRED = "activation-required"
    ACTIVATION_KEY = "activation-key"
    ACTIVATION_SUCCESS = "activation-success"
    ACTIVATION_KEY_NAME = "activation_key"


class UpdateCallsConstants:
    """Constants for auto-update WebSocket messages."""
    VERSION_CHECK = "version-check"
    VERSION_CHECK_RESPONSE = "version-check-response"
    DOWNLOAD_REQUEST = "download-request"
    DOWNLOAD_RESPONSE = "download-response"
    UPDATE_PROGRESS = "update-progress"
    UPDATE_READY = "update-ready"
    UPDATE_ERROR = "update-error"


class ApplicationPlatformConstants:
    WINDOWS = "windows"
    DARWIN = "darwin"
    MACOS = "macos"
    LINUX = "linux"
