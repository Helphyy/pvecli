"""Proxmox VE API client."""

import asyncio
from typing import Any

import httpx

from .auth import AuthHandler
from .exceptions import (
    APIError,
    AuthenticationError,
    NetworkError,
    PermissionError,
    PVECliError,
    ResourceNotFoundError,
    TimeoutError,
)
from ..models.config import ProfileConfig


class ProxmoxClient:
    """Async client for Proxmox VE API."""

    def __init__(self, profile: ProfileConfig) -> None:
        """Initialize Proxmox client.

        Args:
            profile: Profile configuration
        """
        self.profile = profile
        self.base_url = f"https://{profile.host}:{profile.port}/api2/json"
        self.auth_handler = AuthHandler(
            host=profile.host,
            port=profile.port,
            user=profile.auth.user,
            verify_ssl=profile.verify_ssl,
            timeout=profile.timeout,
        )
        self._headers: dict[str, str] | None = None
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "ProxmoxClient":
        """Async context manager entry.

        Returns:
            Self
        """
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit.

        Args:
            exc_type: Exception type
            exc_val: Exception value
            exc_tb: Exception traceback
        """
        await self.close()

    async def connect(self) -> None:
        """Establish connection and authenticate."""
        if self.profile.auth.type == "token":
            if not self.profile.auth.token_name or not self.profile.auth.token_value:
                raise AuthenticationError("Token name and value required for token auth")

            self._headers = self.auth_handler.get_token_headers(
                self.profile.auth.token_name, self.profile.auth.token_value
            )
        else:
            if not self.profile.auth.password:
                raise AuthenticationError("Password required for password auth")

            self._headers = await self.auth_handler.authenticate_with_password(
                self.profile.auth.password
            )

        self._client = httpx.AsyncClient(
            verify=self.profile.verify_ssl, timeout=self.profile.timeout
        )

        await self.auth_handler.verify_authentication(self._headers)

    async def close(self) -> None:
        """Close the client connection."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def get_fresh_ticket(self) -> str:
        """Get a fresh authentication ticket for web console.

        Only works with password authentication. For token auth,
        returns None.

        Returns:
            Fresh ticket string or None for token auth
        """
        if self.profile.auth.type == "password" and self.profile.auth.password:
            return await self.auth_handler.get_fresh_ticket(self.profile.auth.password)
        return None

    def _ensure_connected(self) -> httpx.AsyncClient:
        """Ensure client is connected.

        Returns:
            HTTP client

        Raises:
            RuntimeError: If not connected
        """
        if not self._client or not self._headers:
            raise RuntimeError("Client not connected. Use async with or call connect().")
        return self._client

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        retry_count: int = 3,
    ) -> Any:
        """Make an API request with retry logic.

        Args:
            method: HTTP method
            endpoint: API endpoint (without /api2/json prefix)
            params: Query parameters
            data: Request body data
            retry_count: Number of retries for transient failures

        Returns:
            Response data

        Raises:
            APIError: On API errors
            NetworkError: On network errors
            TimeoutError: On timeout
        """
        client = self._ensure_connected()
        url = f"{self.base_url}/{endpoint.lstrip('/')}"

        for attempt in range(retry_count):
            try:
                response = await client.request(
                    method, url, headers=self._headers, params=params, data=data
                )

                if response.status_code == 401:
                    raise AuthenticationError("Authentication failed or expired")
                elif response.status_code == 403:
                    raise PermissionError("Permission denied for this operation")
                elif response.status_code == 404:
                    raise ResourceNotFoundError("resource", endpoint)
                elif response.status_code >= 400:
                    error_msg = self._extract_error_message(response)
                    raise APIError(error_msg, status_code=response.status_code)

                response.raise_for_status()
                result = response.json()

                return result.get("data")

            except httpx.TimeoutException:
                if attempt < retry_count - 1:
                    await asyncio.sleep(2**attempt)
                    continue
                raise TimeoutError(f"Request to {endpoint} timed out")

            except httpx.NetworkError as e:
                if attempt < retry_count - 1:
                    await asyncio.sleep(2**attempt)
                    continue
                raise NetworkError(f"Network error: {e}")

            except (AuthenticationError, PermissionError, ResourceNotFoundError, APIError):
                raise

            except Exception as e:
                raise APIError(f"Unexpected error: {e}")

        raise APIError("Max retries exceeded")

    def _extract_error_message(self, response: httpx.Response) -> str:
        """Extract error message from response.

        Args:
            response: HTTP response

        Returns:
            Error message
        """
        try:
            data = response.json()
            if "errors" in data:
                return "; ".join(str(v) for v in data["errors"].values())
            return data.get("message", response.text)
        except Exception:
            return response.text or f"HTTP {response.status_code}"

    async def get(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Make a GET request.

        Args:
            endpoint: API endpoint
            params: Query parameters

        Returns:
            Response data
        """
        return await self._request("GET", endpoint, params=params)

    async def post(
        self,
        endpoint: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Make a POST request.

        Args:
            endpoint: API endpoint
            data: Request body data
            params: Query parameters

        Returns:
            Response data
        """
        return await self._request("POST", endpoint, params=params, data=data)

    async def put(
        self,
        endpoint: str,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Make a PUT request.

        Args:
            endpoint: API endpoint
            data: Request body data
            params: Query parameters

        Returns:
            Response data
        """
        return await self._request("PUT", endpoint, params=params, data=data)

    async def delete(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Make a DELETE request.

        Args:
            endpoint: API endpoint
            params: Query parameters

        Returns:
            Response data
        """
        return await self._request("DELETE", endpoint, params=params)

    async def get_version(self) -> dict[str, Any]:
        """Get Proxmox VE version info.

        Returns:
            Version information
        """
        return await self.get("/version")

    async def get_nodes(self) -> list[dict[str, Any]]:
        """Get list of cluster nodes.

        Returns:
            List of nodes
        """
        return await self.get("/nodes")

    async def get_node_status(self, node: str) -> dict[str, Any]:
        """Get status of a specific node.

        Args:
            node: Node name

        Returns:
            Node status
        """
        return await self.get(f"/nodes/{node}/status")

    async def create_vnc_shell(self, node: str, websocket: bool = True) -> dict[str, Any]:
        """Create a VNC shell proxy to a node.

        Args:
            node: Node name
            websocket: Use websocket instead of standard VNC

        Returns:
            VNC connection info (ticket, port, upid, user, etc.)
        """
        data: dict[str, Any] = {"websocket": 1} if websocket else {}
        return await self.post(f"/nodes/{node}/vncshell", data=data)

    async def create_termproxy(self, node: str) -> dict[str, Any]:
        """Create a terminal proxy to a node.

        Args:
            node: Node name

        Returns:
            Terminal connection info (ticket, port, upid, user, etc.)
        """
        return await self.post(f"/nodes/{node}/termproxy", data={})

    async def create_vm_termproxy(self, node: str, vmid: int) -> dict[str, Any]:
        """Create a terminal proxy to a VM (via QEMU guest agent).

        Args:
            node: Node name
            vmid: VM ID

        Returns:
            Terminal connection info (ticket, port, upid, user, etc.)
        """
        return await self.post(f"/nodes/{node}/qemu/{vmid}/termproxy", data={})

    async def create_ct_termproxy(self, node: str, vmid: int) -> dict[str, Any]:
        """Create a terminal proxy to a container.

        Args:
            node: Node name
            vmid: Container ID

        Returns:
            Terminal connection info (ticket, port, upid, user, etc.)
        """
        return await self.post(f"/nodes/{node}/lxc/{vmid}/termproxy", data={})

    async def create_vm_vncproxy(
        self, node: str, vmid: int, websocket: bool = True, generate_password: bool = False
    ) -> dict[str, Any]:
        """Create a VNC proxy connection to a VM.

        Args:
            node: Node name
            vmid: VM ID
            websocket: Use websocket instead of standard VNC
            generate_password: Generate a one-time VNC password

        Returns:
            VNC connection info (ticket, port, upid, user, password if generated)
        """
        data: dict[str, Any] = {}
        if websocket:
            data["websocket"] = 1
        if generate_password:
            data["generate-password"] = 1
        return await self.post(f"/nodes/{node}/qemu/{vmid}/vncproxy", data=data)

    async def create_ct_vncproxy(self, node: str, vmid: int, websocket: bool = True) -> dict[str, Any]:
        """Create a VNC proxy connection to a container.

        Args:
            node: Node name
            vmid: Container ID
            websocket: Use websocket instead of standard VNC

        Returns:
            VNC connection info (ticket, port, upid, user, etc.)
        """
        data = {"websocket": 1} if websocket else {}
        return await self.post(f"/nodes/{node}/lxc/{vmid}/vncproxy", data=data)

    async def get_cluster_resources(
        self, resource_type: str | None = None
    ) -> list[dict[str, Any]]:
        """Get cluster resources.

        Args:
            resource_type: Filter by type (vm, storage, node, etc.)

        Returns:
            List of resources
        """
        params = {"type": resource_type} if resource_type else None
        return await self.get("/cluster/resources", params=params)

    # VM (QEMU) methods

    async def get_vms(self, node: str | None = None) -> list[dict[str, Any]]:
        """Get list of VMs.

        Args:
            node: Optional node name to filter VMs

        Returns:
            List of VMs
        """
        if node:
            return await self.get(f"/nodes/{node}/qemu")
        else:
            resources = await self.get_cluster_resources(resource_type="vm")
            return [r for r in resources if r.get("type") == "qemu"]

    async def get_vm_status(self, node: str, vmid: int) -> dict[str, Any]:
        """Get current status of a VM.

        Args:
            node: Node name
            vmid: VM ID

        Returns:
            VM status
        """
        return await self.get(f"/nodes/{node}/qemu/{vmid}/status/current")

    async def get_vm_config(self, node: str, vmid: int) -> dict[str, Any]:
        """Get VM configuration.

        Args:
            node: Node name
            vmid: VM ID

        Returns:
            VM configuration
        """
        return await self.get(f"/nodes/{node}/qemu/{vmid}/config")

    async def get_vm_interfaces(self, node: str, vmid: int) -> list[dict[str, Any]]:
        """Get VM network interfaces via QEMU Guest Agent.

        Args:
            node: Node name
            vmid: VM ID

        Returns:
            List of network interfaces with IP information
        """
        try:
            response = await self.get(f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces")
            # The API returns a 'result' property with the interfaces list
            if isinstance(response, dict):
                if "result" in response:
                    result = response.get("result", {})
                    if isinstance(result, dict) and "interfaces" in result:
                        return result.get("interfaces", [])
                    return result if isinstance(result, list) else []
                elif "interfaces" in response:
                    return response.get("interfaces", [])
                elif isinstance(response, list):
                    return response
            elif isinstance(response, list):
                return response
            return []
        except (APIError, ResourceNotFoundError, NetworkError):
            # If the endpoint doesn't exist or fails, return empty list
            # (QEMU Guest Agent might not be available)
            return []
        except Exception:
            # Silently fail for other errors
            return []

    async def exec_vm_command(
        self, node: str, vmid: int, command: list[str], input_data: str | None = None
    ) -> dict[str, Any]:
        """Execute a command in a VM via QEMU Guest Agent.

        Args:
            node: Node name
            vmid: VM ID
            command: Command as list of strings (program + arguments)
            input_data: Optional input data to pass to the command (stdin).
                       Will be base64 encoded if provided.

        Returns:
            Result with PID

        Raises:
            APIError: If command execution fails
        """
        import base64

        data: dict[str, Any] = {"command": command}
        if input_data:
            # Proxmox expects input-data to be base64 encoded
            encoded_input = base64.b64encode(input_data.encode("utf-8")).decode("utf-8")
            data["input-data"] = encoded_input

        return await self.post(f"/nodes/{node}/qemu/{vmid}/agent/exec", data=data)

    async def get_vm_exec_status(self, node: str, vmid: int, pid: int) -> dict[str, Any]:
        """Get the status and output of a command executed in a VM.

        Args:
            node: Node name
            vmid: VM ID
            pid: Process ID returned from exec_vm_command

        Returns:
            Status and output of the command

        Raises:
            APIError: If status retrieval fails
        """
        return await self.get(f"/nodes/{node}/qemu/{vmid}/agent/exec-status", params={"pid": pid})

    async def get_vm_osinfo(self, node: str, vmid: int) -> dict[str, Any]:
        """Get OS information from a VM via QEMU Guest Agent.

        Args:
            node: Node name
            vmid: VM ID

        Returns:
            OS information (name, version, etc.)
        """
        try:
            response = await self.get(f"/nodes/{node}/qemu/{vmid}/agent/get-osinfo")
            # The API returns a 'result' property with the OS info
            if isinstance(response, dict) and "result" in response:
                return response.get("result", {})
            return response if isinstance(response, dict) else {}
        except (APIError, ResourceNotFoundError, NetworkError):
            # If the endpoint doesn't exist or fails, return empty dict
            # (QEMU Guest Agent might not be available)
            return {}
        except Exception:
            # Silently fail for other errors
            return {}

    async def start_vm(self, node: str, vmid: int) -> str:
        """Start a VM.

        Args:
            node: Node name
            vmid: VM ID

        Returns:
            Task UPID
        """
        return await self.post(f"/nodes/{node}/qemu/{vmid}/status/start")

    async def stop_vm(self, node: str, vmid: int, timeout: int | None = None) -> str:
        """Stop a VM (hard stop).

        Args:
            node: Node name
            vmid: VM ID
            timeout: Timeout in seconds

        Returns:
            Task UPID
        """
        data = {"timeout": timeout} if timeout else None
        return await self.post(f"/nodes/{node}/qemu/{vmid}/status/stop", data=data)

    async def shutdown_vm(
        self, node: str, vmid: int, timeout: int | None = None, force_stop: bool = False
    ) -> str:
        """Shutdown a VM gracefully.

        Args:
            node: Node name
            vmid: VM ID
            timeout: Timeout in seconds before force stop
            force_stop: Force stop after timeout

        Returns:
            Task UPID
        """
        data = {}
        if timeout:
            data["timeout"] = timeout
        if force_stop:
            data["forceStop"] = 1
        return await self.post(f"/nodes/{node}/qemu/{vmid}/status/shutdown", data=data or None)

    async def reboot_vm(self, node: str, vmid: int, timeout: int | None = None) -> str:
        """Reboot a VM.

        Args:
            node: Node name
            vmid: VM ID
            timeout: Timeout in seconds

        Returns:
            Task UPID
        """
        data = {"timeout": timeout} if timeout else None
        return await self.post(f"/nodes/{node}/qemu/{vmid}/status/reboot", data=data)

    async def suspend_vm(self, node: str, vmid: int) -> str:
        """Suspend a VM.

        Args:
            node: Node name
            vmid: VM ID

        Returns:
            Task UPID
        """
        return await self.post(f"/nodes/{node}/qemu/{vmid}/status/suspend")

    async def resume_vm(self, node: str, vmid: int) -> str:
        """Resume a suspended VM.

        Args:
            node: Node name
            vmid: VM ID

        Returns:
            Task UPID
        """
        return await self.post(f"/nodes/{node}/qemu/{vmid}/status/resume")

    async def reset_vm(self, node: str, vmid: int) -> str:
        """Reset a VM (hard reset).

        Args:
            node: Node name
            vmid: VM ID

        Returns:
            Task UPID
        """
        return await self.post(f"/nodes/{node}/qemu/{vmid}/status/reset")

    async def clone_vm(
        self,
        node: str,
        vmid: int,
        newid: int,
        name: str | None = None,
        target: str | None = None,
        full: bool = False,
        snapname: str | None = None,
        pool: str | None = None,
        storage: str | None = None,
        format: str | None = None,
        description: str | None = None,
    ) -> str:
        """Clone a VM.

        Args:
            node: Source node name
            vmid: Source VM ID
            newid: New VM ID
            name: New VM name
            target: Target node
            full: Create full clone
            snapname: Clone from snapshot
            pool: Add to pool
            storage: Target storage
            format: Target format
            description: VM description

        Returns:
            Task UPID
        """
        data: dict[str, Any] = {"newid": newid}
        if name:
            data["name"] = name
        if target:
            data["target"] = target
        if full:
            data["full"] = 1
        if snapname:
            data["snapname"] = snapname
        if pool:
            data["pool"] = pool
        if storage:
            data["storage"] = storage
        if format:
            data["format"] = format
        if description:
            data["description"] = description

        return await self.post(f"/nodes/{node}/qemu/{vmid}/clone", data=data)

    async def create_vm(
        self,
        node: str,
        vmid: int,
        **config_params: Any,
    ) -> str:
        """Create a new VM.

        Args:
            node: Node name
            vmid: VM ID
            **config_params: VM configuration parameters (name, memory, cores, etc.)

        Returns:
            Task UPID
        """
        data: dict[str, Any] = {"vmid": vmid}

        for key, value in config_params.items():
            if value is not None:
                data[key] = value

        return await self.post(f"/nodes/{node}/qemu", data=data)

    async def update_vm_config(
        self,
        node: str,
        vmid: int,
        **config_params: Any,
    ) -> None:
        """Update VM configuration.

        Args:
            node: Node name
            vmid: VM ID
            **config_params: Configuration parameters (sockets, cores, memory, etc.)

        Returns:
            None (synchronous operation)
        """
        if not config_params:
            return

        data: dict[str, Any] = {}
        for key, value in config_params.items():
            if value is not None:
                data[key] = value

        if data:
            await self.put(f"/nodes/{node}/qemu/{vmid}/config", data=data)

    async def resize_vm_disk(self, node: str, vmid: int, disk: str, size: str) -> None:
        """Resize a VM disk.

        Args:
            node: Node name
            vmid: VM ID
            disk: Disk name (e.g. scsi0, virtio0)
            size: New size (e.g. '50G' or '+10G')
        """
        await self.put(f"/nodes/{node}/qemu/{vmid}/resize", data={"disk": disk, "size": size})

    async def delete_vm(self, node: str, vmid: int, purge: bool = False) -> str:
        """Delete a VM.

        Args:
            node: Node name
            vmid: VM ID
            purge: Also remove from backup jobs and HA config

        Returns:
            Task UPID
        """
        params = {"purge": 1} if purge else None
        return await self.delete(f"/nodes/{node}/qemu/{vmid}", params=params)

    # Snapshot methods

    async def get_vm_snapshots(self, node: str, vmid: int) -> list[dict[str, Any]]:
        """Get list of VM snapshots.

        Args:
            node: Node name
            vmid: VM ID

        Returns:
            List of snapshots
        """
        return await self.get(f"/nodes/{node}/qemu/{vmid}/snapshot")

    async def create_vm_snapshot(
        self,
        node: str,
        vmid: int,
        snapname: str,
        description: str | None = None,
        vmstate: bool = False,
    ) -> str:
        """Create a VM snapshot.

        Args:
            node: Node name
            vmid: VM ID
            snapname: Snapshot name
            description: Snapshot description
            vmstate: Include RAM state

        Returns:
            Task UPID
        """
        data: dict[str, Any] = {"snapname": snapname}
        if description:
            data["description"] = description
        if vmstate:
            data["vmstate"] = 1

        return await self.post(f"/nodes/{node}/qemu/{vmid}/snapshot", data=data)

    async def rollback_vm_snapshot(
        self, node: str, vmid: int, snapname: str
    ) -> str:
        """Rollback to a VM snapshot.

        Args:
            node: Node name
            vmid: VM ID
            snapname: Snapshot name

        Returns:
            Task UPID
        """
        return await self.post(f"/nodes/{node}/qemu/{vmid}/snapshot/{snapname}/rollback")

    async def delete_vm_snapshot(
        self, node: str, vmid: int, snapname: str, force: bool = False
    ) -> str:
        """Delete a VM snapshot.

        Args:
            node: Node name
            vmid: VM ID
            snapname: Snapshot name
            force: Force deletion

        Returns:
            Task UPID
        """
        params = {"force": 1} if force else None
        return await self.delete(
            f"/nodes/{node}/qemu/{vmid}/snapshot/{snapname}", params=params
        )

    # Task methods

    async def get_task_status(self, node: str, upid: str) -> dict[str, Any]:
        """Get status of a task.

        Args:
            node: Node name
            upid: Task UPID

        Returns:
            Task status
        """
        return await self.get(f"/nodes/{node}/tasks/{upid}/status")

    async def wait_for_task(
        self, node: str, upid: str, timeout: int = 300, poll_interval: float = 2.0
    ) -> dict[str, Any]:
        """Wait for a task to complete with Ctrl+C support.

        Args:
            node: Node name
            upid: Task UPID
            timeout: Maximum wait time in seconds
            poll_interval: Polling interval in seconds

        Returns:
            Final task status

        Raises:
            TimeoutError: If task doesn't complete within timeout
            APIError: If task fails
            asyncio.CancelledError: If interrupted by Ctrl+C
        """
        import signal
        import time

        start_time = time.time()
        task = None
        old_handler = None

        try:
            # Get current task for signal handling
            task = asyncio.current_task()

            # Handle Ctrl+C by cancelling the wait
            def signal_handler(signum: int, frame: Any) -> None:
                if task and not task.done():
                    task.cancel()

            old_handler = signal.signal(signal.SIGINT, signal_handler)

            while True:
                status = await self.get_task_status(node, upid)

                if status.get("status") == "stopped":
                    exitstatus = status.get("exitstatus", "")
                    if exitstatus != "OK":
                        raise APIError(f"Task failed with status: {exitstatus}")
                    return status

                if time.time() - start_time > timeout:
                    raise TimeoutError(f"Task {upid} did not complete within {timeout} seconds")

                await asyncio.sleep(poll_interval)
        finally:
            if old_handler is not None:
                signal.signal(signal.SIGINT, old_handler)

    async def stop_task(self, node: str, upid: str) -> None:
        """Stop a running task.

        Args:
            node: Node name
            upid: Task UPID
        """
        try:
            await self.delete(f"/nodes/{node}/tasks/{upid}")
        except Exception:
            # Task might already be stopped, ignore errors
            pass

    # Container (LXC) methods

    async def get_containers(self, node: str | None = None) -> list[dict[str, Any]]:
        """Get list of containers.

        Args:
            node: Optional node name to filter containers

        Returns:
            List of containers
        """
        if node:
            return await self.get(f"/nodes/{node}/lxc")
        else:
            resources = await self.get_cluster_resources(resource_type="vm")
            return [r for r in resources if r.get("type") == "lxc"]

    async def get_container_status(self, node: str, vmid: int) -> dict[str, Any]:
        """Get current status of a container.

        Args:
            node: Node name
            vmid: Container ID

        Returns:
            Container status
        """
        return await self.get(f"/nodes/{node}/lxc/{vmid}/status/current")

    async def get_container_config(self, node: str, vmid: int) -> dict[str, Any]:
        """Get container configuration.

        Args:
            node: Node name
            vmid: Container ID

        Returns:
            Container configuration
        """
        return await self.get(f"/nodes/{node}/lxc/{vmid}/config")

    async def get_container_interfaces(self, node: str, vmid: int) -> list[dict[str, Any]]:
        """Get container network interfaces.

        Args:
            node: Node name
            vmid: Container ID

        Returns:
            List of network interfaces with IP information
        """
        response = await self.get(f"/nodes/{node}/lxc/{vmid}/interfaces")
        if isinstance(response, dict) and "data" in response:
            return response.get("data", [])
        return response if isinstance(response, list) else []

    async def start_container(self, node: str, vmid: int) -> str:
        """Start a container.

        Args:
            node: Node name
            vmid: Container ID

        Returns:
            Task UPID
        """
        return await self.post(f"/nodes/{node}/lxc/{vmid}/status/start")

    async def stop_container(self, node: str, vmid: int, timeout: int | None = None) -> str:
        """Stop a container (hard stop).

        Args:
            node: Node name
            vmid: Container ID
            timeout: Timeout in seconds

        Returns:
            Task UPID
        """
        data = {"timeout": timeout} if timeout else None
        return await self.post(f"/nodes/{node}/lxc/{vmid}/status/stop", data=data)

    async def shutdown_container(
        self, node: str, vmid: int, timeout: int | None = None, force_stop: bool = False
    ) -> str:
        """Shutdown a container gracefully.

        Args:
            node: Node name
            vmid: Container ID
            timeout: Timeout in seconds before force stop
            force_stop: Force stop after timeout

        Returns:
            Task UPID
        """
        data = {}
        if timeout:
            data["timeout"] = timeout
        if force_stop:
            data["forceStop"] = 1
        return await self.post(
            f"/nodes/{node}/lxc/{vmid}/status/shutdown", data=data or None
        )

    async def reboot_container(self, node: str, vmid: int, timeout: int | None = None) -> str:
        """Reboot a container.

        Args:
            node: Node name
            vmid: Container ID
            timeout: Timeout in seconds

        Returns:
            Task UPID
        """
        data = {"timeout": timeout} if timeout else None
        return await self.post(f"/nodes/{node}/lxc/{vmid}/status/reboot", data=data)

    async def suspend_container(self, node: str, vmid: int) -> str:
        """Suspend a container.

        Args:
            node: Node name
            vmid: Container ID

        Returns:
            Task UPID
        """
        return await self.post(f"/nodes/{node}/lxc/{vmid}/status/suspend")

    async def resume_container(self, node: str, vmid: int) -> str:
        """Resume a suspended container.

        Args:
            node: Node name
            vmid: Container ID

        Returns:
            Task UPID
        """
        return await self.post(f"/nodes/{node}/lxc/{vmid}/status/resume")

    async def clone_container(
        self,
        node: str,
        vmid: int,
        newid: int,
        hostname: str | None = None,
        target: str | None = None,
        full: bool = False,
        snapname: str | None = None,
        pool: str | None = None,
        storage: str | None = None,
        description: str | None = None,
    ) -> str:
        """Clone a container.

        Args:
            node: Source node name
            vmid: Source container ID
            newid: New container ID
            hostname: New container hostname
            target: Target node
            full: Create full clone
            snapname: Clone from snapshot
            pool: Add to pool
            storage: Target storage
            description: Container description

        Returns:
            Task UPID
        """
        data: dict[str, Any] = {"newid": newid}
        if hostname:
            data["hostname"] = hostname
        if target:
            data["target"] = target
        if full:
            data["full"] = 1
        if snapname:
            data["snapname"] = snapname
        if pool:
            data["pool"] = pool
        if storage:
            data["storage"] = storage
        if description:
            data["description"] = description

        return await self.post(f"/nodes/{node}/lxc/{vmid}/clone", data=data)

    async def create_container(
        self,
        node: str,
        vmid: int,
        **config_params: Any,
    ) -> str:
        """Create a new container.

        Args:
            node: Node name
            vmid: Container ID
            **config_params: Container configuration parameters (hostname, ostemplate, memory, cores, etc.)

        Returns:
            Task UPID
        """
        data: dict[str, Any] = {"vmid": vmid}

        for key, value in config_params.items():
            if value is not None:
                data[key] = value

        return await self.post(f"/nodes/{node}/lxc", data=data)

    async def update_container_config(
        self,
        node: str,
        vmid: int,
        **config_params: Any,
    ) -> None:
        """Update container configuration.

        Args:
            node: Node name
            vmid: Container ID
            **config_params: Configuration parameters (cores, memory, tags, etc.)

        Returns:
            None (synchronous operation)
        """
        if not config_params:
            return

        data: dict[str, Any] = {}
        for key, value in config_params.items():
            if value is not None:
                data[key] = value

        if data:
            await self.put(f"/nodes/{node}/lxc/{vmid}/config", data=data)

    async def resize_container_disk(self, node: str, vmid: int, disk: str, size: str) -> None:
        """Resize a container disk.

        Args:
            node: Node name
            vmid: Container ID
            disk: Disk name (e.g. rootfs, mp0)
            size: New size (e.g. '20G' or '+5G')
        """
        await self.put(f"/nodes/{node}/lxc/{vmid}/resize", data={"disk": disk, "size": size})

    async def delete_container(self, node: str, vmid: int, purge: bool = False) -> str:
        """Delete a container.

        Args:
            node: Node name
            vmid: Container ID
            purge: Also remove from backup jobs and HA config

        Returns:
            Task UPID
        """
        params = {"purge": 1} if purge else None
        return await self.delete(f"/nodes/{node}/lxc/{vmid}", params=params)

    # Container snapshot methods

    async def get_container_snapshots(self, node: str, vmid: int) -> list[dict[str, Any]]:
        """Get list of container snapshots.

        Args:
            node: Node name
            vmid: Container ID

        Returns:
            List of snapshots
        """
        return await self.get(f"/nodes/{node}/lxc/{vmid}/snapshot")

    async def create_container_snapshot(
        self,
        node: str,
        vmid: int,
        snapname: str,
        description: str | None = None,
    ) -> str:
        """Create a container snapshot.

        Args:
            node: Node name
            vmid: Container ID
            snapname: Snapshot name
            description: Snapshot description

        Returns:
            Task UPID
        """
        data: dict[str, Any] = {"snapname": snapname}
        if description:
            data["description"] = description

        return await self.post(f"/nodes/{node}/lxc/{vmid}/snapshot", data=data)

    async def rollback_container_snapshot(self, node: str, vmid: int, snapname: str) -> str:
        """Rollback to a container snapshot.

        Args:
            node: Node name
            vmid: Container ID
            snapname: Snapshot name

        Returns:
            Task UPID
        """
        return await self.post(f"/nodes/{node}/lxc/{vmid}/snapshot/{snapname}/rollback")

    async def delete_container_snapshot(
        self, node: str, vmid: int, snapname: str, force: bool = False
    ) -> str:
        """Delete a container snapshot.

        Args:
            node: Node name
            vmid: Container ID
            snapname: Snapshot name
            force: Force deletion

        Returns:
            Task UPID
        """
        params = {"force": 1} if force else None
        return await self.delete(f"/nodes/{node}/lxc/{vmid}/snapshot/{snapname}", params=params)

    # Storage methods

    async def get_storage_list(self, node: str) -> list[dict[str, Any]]:
        """Get list of storage on a node.

        Args:
            node: Node name

        Returns:
            List of storage
        """
        return await self.get(f"/nodes/{node}/storage")

    async def get_storage_status(self, node: str, storage: str) -> dict[str, Any]:
        """Get storage status.

        Args:
            node: Node name
            storage: Storage ID

        Returns:
            Storage status
        """
        return await self.get(f"/nodes/{node}/storage/{storage}/status")

    async def get_storage_content(
        self, node: str, storage: str, content_type: str | None = None
    ) -> list[dict[str, Any]]:
        """Get storage content.

        Args:
            node: Node name
            storage: Storage ID
            content_type: Filter by content type (images, vztmpl, backup, etc.)

        Returns:
            List of content items
        """
        params = {"content": content_type} if content_type else None
        return await self.get(f"/nodes/{node}/storage/{storage}/content", params=params)

    # Cluster methods (extended)

    async def get_cluster_status(self) -> list[dict[str, Any]]:
        """Get cluster status.

        Returns:
            Cluster status information
        """
        return await self.get("/cluster/status")

    async def get_cluster_tasks(
        self, running: bool = False, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Get cluster tasks.

        Args:
            running: Only show running tasks
            limit: Maximum number of tasks to return

        Returns:
            List of tasks
        """
        import asyncio

        nodes = await self.get_nodes()
        node_names = [n.get("node", "") for n in nodes if n.get("node")]

        params: dict[str, Any] = {"limit": limit}
        if running:
            params["source"] = "active"

        results = await asyncio.gather(
            *(self.get(f"/nodes/{node}/tasks", params=params) for node in node_names)
        )

        all_tasks: list[dict[str, Any]] = []
        for node_name, tasks in zip(node_names, results):
            for t in tasks:
                t.setdefault("node", node_name)
                all_tasks.append(t)

        # Sort by start time descending
        all_tasks.sort(key=lambda t: t.get("starttime", 0), reverse=True)
        return all_tasks[:limit]

    async def get_cluster_backup_schedule(self) -> list[dict[str, Any]]:
        """Get cluster backup schedule.

        Returns:
            List of backup jobs
        """
        return await self.get("/cluster/backup")

    async def get_cluster_ha_status(self) -> dict[str, Any]:
        """Get cluster HA status.

        Returns:
            HA status information
        """
        return await self.get("/cluster/ha/status/current")

    async def get_cluster_options(self) -> dict[str, Any]:
        """Get cluster options.

        Returns:
            Cluster options
        """
        return await self.get("/cluster/options")

    async def update_cluster_options(self, **params: Any) -> None:
        """Update cluster options."""
        data = {k: v for k, v in params.items() if v is not None}
        if data:
            await self.put("/cluster/options", data=data)

    async def get_next_vmid(self) -> int:
        """Get next available VMID.

        Returns:
            Next available VMID
        """
        result = await self.get("/cluster/nextid")
        return int(result)

    async def get_pools(self) -> list[dict[str, Any]]:
        """Get list of resource pools.

        Returns:
            List of pools
        """
        return await self.get("/pools")

    async def get_network_interfaces(self, node: str) -> list[dict[str, Any]]:
        """Get network interfaces/bridges on a node.

        Args:
            node: Node name

        Returns:
            List of network interfaces
        """
        return await self.get(f"/nodes/{node}/network")

    async def get_storage_config(self, storage: str) -> dict[str, Any]:
        """Get storage configuration.

        Args:
            storage: Storage ID

        Returns:
            Storage configuration
        """
        return await self.get(f"/storage/{storage}")

    async def update_storage_config(
        self,
        storage: str,
        **config_params: Any,
    ) -> None:
        """Update storage configuration.

        Args:
            storage: Storage ID
            **config_params: Configuration parameters (content, nodes, disable, etc.)

        Returns:
            None (synchronous operation)
        """
        if not config_params:
            return

        data: dict[str, Any] = {}
        for key, value in config_params.items():
            if value is not None:
                data[key] = value

        if data:
            await self.put(f"/storage/{storage}", data=data)

    async def delete_storage_content(
        self,
        node: str,
        storage: str,
        volume: str,
    ) -> None:
        """Delete content from storage.

        Args:
            node: Node name
            storage: Storage ID
            volume: Volume ID (e.g., 'local:vztmpl/debian-12-standard.tar.zst')

        Returns:
            None
        """
        await self.delete(f"/nodes/{node}/storage/{storage}/content/{volume}")

    async def get_available_templates(
        self,
        node: str,
        section: str = "system",
    ) -> list[dict[str, Any]]:
        """Get list of available templates from Proxmox Appliance Manager.

        Args:
            node: Node name
            section: Template section (system, turnkey, etc.) - default: system

        Returns:
            List of available templates
        """
        # Execute pveam available command on the node
        data = {
            "commands": [
                {
                    "method": "GET",
                    "path": f"/nodes/{node}/aplinfo",
                }
            ]
        }

        try:
            # Try to get appliance list
            result = await self.get(f"/nodes/{node}/aplinfo")
            return result if isinstance(result, list) else []
        except Exception:
            # Fallback: return empty list if API call fails
            return []

    async def download_template(
        self,
        node: str,
        storage: str,
        template: str,
        template_data: dict[str, Any] | None = None,
    ) -> str:
        """Download a template from the Proxmox template repository.

        Args:
            node: Node name
            storage: Storage ID
            template: Template filename (e.g., 'debian-12-standard_12.7-1_amd64.tar.zst')
            template_data: Full template data from API (contains download URL)

        Returns:
            Task UPID
        """
        # Try to get the download URL from template data
        url = None
        if template_data:
            # Look for URL field in template data
            url = template_data.get("url") or template_data.get("location")

        # Fallback: construct URL if not found in template data
        if not url:
            url = f"https://www.proxmox.com/appliances/get/{template}"

        data = {
            "url": url,
            "content": "vztmpl",
            "filename": template,
        }
        return await self.post(f"/nodes/{node}/storage/{storage}/download-url", data=data)

    async def upload_storage_content(
        self,
        node: str,
        storage: str,
        content_type: str,
        file_path: str,
        filename: str | None = None,
        checksum: str | None = None,
        checksum_algorithm: str | None = None,
    ) -> str:
        """Upload content to storage.

        Args:
            node: Node name
            storage: Storage ID
            content_type: Content type (iso, vztmpl, import)
            file_path: Path to file to upload
            filename: Target filename (defaults to source filename)
            checksum: Expected checksum of the file
            checksum_algorithm: Algorithm to calculate checksum (md5, sha1, sha256, etc.)

        Returns:
            Upload task ID (UPID)
        """
        client = self._ensure_connected()
        url = f"{self.base_url}/nodes/{node}/storage/{storage}/upload"

        # Prepare form data
        from pathlib import Path

        file = Path(file_path)
        if not file.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        if filename is None:
            filename = file.name

        # Build multipart form data
        data = {"content": content_type}

        if checksum:
            data["checksum"] = checksum
        if checksum_algorithm:
            data["checksum-algorithm"] = checksum_algorithm

        # Open file in context manager to ensure it's properly closed
        try:
            with open(file_path, "rb") as f:
                files = {"filename": (filename, f, "application/octet-stream")}

                response = await client.request(
                    "POST", url, headers=self._headers, data=data, files=files
                )

                if response.status_code == 401:
                    raise AuthenticationError("Authentication failed or expired")
                elif response.status_code == 403:
                    raise PermissionError("Permission denied for this operation")
                elif response.status_code == 404:
                    raise ResourceNotFoundError("resource", f"/nodes/{node}/storage/{storage}")
                elif response.status_code >= 400:
                    error_msg = self._extract_error_message(response)
                    raise APIError(error_msg, status_code=response.status_code)

                response.raise_for_status()
                result = response.json()

                return result.get("data", "")

        except httpx.TimeoutException:
            raise TimeoutError(f"Upload to {storage} timed out")
        except httpx.NetworkError as e:
            raise NetworkError(f"Network error during upload: {e}")
        except (AuthenticationError, PermissionError, ResourceNotFoundError, APIError):
            raise
        except Exception as e:
            raise APIError(f"Unexpected error during upload: {e}")
