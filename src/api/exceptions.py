"""Custom exceptions for pvecli API interactions."""


class PVECliError(Exception):
    """Base exception for pvecli."""

    pass


class ConfigError(PVECliError):
    """Configuration related errors."""

    pass


class AuthenticationError(PVECliError):
    """Authentication failures."""

    pass


class APIError(PVECliError):
    """General API errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """Initialize API error.

        Args:
            message: Error message
            status_code: HTTP status code if applicable
        """
        super().__init__(message)
        self.status_code = status_code


class ResourceNotFoundError(APIError):
    """Resource not found (404)."""

    def __init__(self, resource: str, identifier: str) -> None:
        """Initialize resource not found error.

        Args:
            resource: Type of resource (vm, node, etc.)
            identifier: Resource identifier
        """
        super().__init__(f"{resource} '{identifier}' not found", status_code=404)
        self.resource = resource
        self.identifier = identifier


class PermissionError(APIError):
    """Permission denied (403)."""

    def __init__(self, message: str = "Permission denied") -> None:
        """Initialize permission error.

        Args:
            message: Error message
        """
        super().__init__(message, status_code=403)


class NetworkError(PVECliError):
    """Network related errors."""

    pass


class TimeoutError(PVECliError):
    """Request timeout errors."""

    pass
