"""Authentication handling for Proxmox VE API."""

from typing import Any

import httpx

from .exceptions import AuthenticationError


class AuthHandler:
    """Handle authentication for Proxmox VE API."""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        verify_ssl: bool = True,
        timeout: int = 30,
    ) -> None:
        """Initialize auth handler.

        Args:
            host: Proxmox host
            port: Proxmox port
            user: Username
            verify_ssl: Whether to verify SSL certificates
            timeout: Request timeout in seconds
        """
        self.host = host
        self.port = port
        self.user = user
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.base_url = f"https://{host}:{port}/api2/json"

    def get_token_headers(self, token_name: str, token_value: str) -> dict[str, str]:
        """Get headers for API token authentication.

        Args:
            token_name: Token name
            token_value: Token value/UUID

        Returns:
            Headers dict with Authorization
        """
        return {"Authorization": f"PVEAPIToken={self.user}!{token_name}={token_value}"}

    async def authenticate_with_password(self, password: str) -> dict[str, str]:
        """Authenticate using username and password to get a ticket.

        Args:
            password: User password

        Returns:
            Headers dict with ticket and CSRF token

        Raises:
            AuthenticationError: If authentication fails
        """
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=self.timeout) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/access/ticket",
                    data={"username": self.user, "password": password},
                )

                if response.status_code == 401:
                    raise AuthenticationError("Invalid username or password")

                response.raise_for_status()
                data = response.json()["data"]

                return {
                    "Cookie": f"PVEAuthCookie={data['ticket']}",
                    "CSRFPreventionToken": data["CSRFPreventionToken"],
                }

            except httpx.HTTPStatusError as e:
                raise AuthenticationError(f"Authentication failed: {e}")
            except httpx.RequestError as e:
                raise AuthenticationError(f"Connection failed: {e}")
            except KeyError:
                raise AuthenticationError("Invalid response from server")

    async def verify_authentication(self, headers: dict[str, str]) -> bool:
        """Verify authentication is valid by making a test request.

        Args:
            headers: Authentication headers

        Returns:
            True if authentication is valid

        Raises:
            AuthenticationError: If authentication verification fails
        """
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=self.timeout) as client:
            try:
                response = await client.get(f"{self.base_url}/version", headers=headers)

                if response.status_code == 401:
                    raise AuthenticationError("Authentication invalid or expired")

                response.raise_for_status()
                return True

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401:
                    raise AuthenticationError("Authentication invalid or expired")
                raise AuthenticationError(f"Verification failed: {e}")
            except httpx.RequestError as e:
                raise AuthenticationError(f"Connection failed: {e}")

    async def get_fresh_ticket(self, password: str) -> str:
        """Get a fresh authentication ticket.

        This method creates a new ticket directly, useful for operations
        that need a valid ticket (like opening web console).

        Args:
            password: User password

        Returns:
            Fresh ticket string

        Raises:
            AuthenticationError: If authentication fails
        """
        async with httpx.AsyncClient(verify=self.verify_ssl, timeout=self.timeout) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/access/ticket",
                    data={"username": self.user, "password": password},
                )

                if response.status_code == 401:
                    raise AuthenticationError("Invalid username or password")

                response.raise_for_status()
                data = response.json()["data"]

                return data["ticket"]

            except httpx.HTTPStatusError as e:
                raise AuthenticationError(f"Authentication failed: {e}")
            except httpx.RequestError as e:
                raise AuthenticationError(f"Connection failed: {e}")
            except KeyError:
                raise AuthenticationError("Invalid response from server")
