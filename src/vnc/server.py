"""Local VNC proxy server with embedded noVNC client."""

import asyncio
import mimetypes
import ssl
import sys
import time
from http import HTTPStatus
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlparse

import websockets.exceptions
from websockets.asyncio.client import connect
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

NOVNC_ROOT = Path(__file__).parent / "novnc"


class VNCProxyServer:
    """Local HTTP+WebSocket server that serves noVNC and proxies VNC to Proxmox."""

    # Grace period (seconds) after last connection drops before exiting
    DISCONNECT_GRACE = 5

    def __init__(
        self,
        proxmox_host: str,
        proxmox_port: int,
        ws_path: str,
        vncticket: str,
        pve_port: int,
        auth_headers: dict[str, str],
        local_port: int,
        verify_ssl: bool = False,
        vnc_password: str | None = None,
    ) -> None:
        self.proxmox_host = proxmox_host
        self.proxmox_port = proxmox_port
        self.ws_path = ws_path
        self.vncticket = vncticket
        self.pve_port = pve_port
        self.local_port = local_port
        self.verify_ssl = verify_ssl
        self.vnc_password = vnc_password
        self._active_connections = 0
        self._ever_connected = False
        self._last_disconnect: float = 0

        # Only keep Cookie header for WebSocket auth (CSRF token breaks WS upgrade)
        self.ws_headers: dict[str, str] = {}
        for key, value in auth_headers.items():
            if key.lower() in ("cookie", "authorization"):
                self.ws_headers[key] = value

    def get_browser_url(self) -> str:
        url = f"http://localhost:{self.local_port}/vnc.html?path=vnc-proxy&resize=scale&autoconnect=true"
        if self.vnc_password:
            url += f"&password={quote(self.vnc_password, safe='')}"
        return url

    # -- HTTP static file serving --

    async def process_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        parsed = urlparse(request.path)
        path = unquote(parsed.path)
        if path == "/vnc-proxy":
            return None  # proceed with WebSocket upgrade
        return self._serve_static(path)

    def _serve_static(self, url_path: str) -> Response:
        if url_path in ("/", ""):
            url_path = "/vnc.html"

        try:
            relative = PurePosixPath(url_path).relative_to("/")
        except ValueError:
            return self._http_error(HTTPStatus.BAD_REQUEST)

        resolved = (NOVNC_ROOT / str(relative)).resolve()
        if not str(resolved).startswith(str(NOVNC_ROOT.resolve())):
            return self._http_error(HTTPStatus.FORBIDDEN)

        try:
            body = resolved.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            return self._http_error(HTTPStatus.NOT_FOUND)

        content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        headers = Headers(
            [("Content-Type", content_type), ("Content-Length", str(len(body)))]
        )
        return Response(HTTPStatus.OK, "OK", headers, body)

    @staticmethod
    def _http_error(status: HTTPStatus) -> Response:
        body = f"{status.value} {status.phrase}\n".encode()
        headers = Headers(
            [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))]
        )
        return Response(status.value, status.phrase, headers, body)

    # -- WebSocket proxy --

    async def handle_vnc_proxy(self, client_ws: ServerConnection) -> None:
        """Relay WebSocket frames between noVNC (client) and Proxmox."""
        self._active_connections += 1
        self._ever_connected = True

        try:
            await self._proxy_to_proxmox(client_ws)
        except Exception:
            pass
        finally:
            self._active_connections -= 1
            self._last_disconnect = time.monotonic()

    async def _proxy_to_proxmox(self, client_ws: ServerConnection) -> None:
        target_url = (
            f"wss://{self.proxmox_host}:{self.proxmox_port}"
            f"{self.ws_path}"
            f"?port={self.pve_port}&vncticket={quote(self.vncticket, safe='')}"
        )

        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if not self.verify_ssl:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        async with connect(
            target_url,
            additional_headers=self.ws_headers,
            ssl=ssl_ctx,
            # Disable library-level ping; Proxmox manages its own keepalive
            ping_interval=None,
        ) as proxmox_ws:

            async def client_to_proxmox() -> None:
                try:
                    async for msg in client_ws:
                        await proxmox_ws.send(msg)
                except websockets.exceptions.ConnectionClosed:
                    pass

            async def proxmox_to_client() -> None:
                try:
                    async for msg in proxmox_ws:
                        await client_ws.send(msg)
                except websockets.exceptions.ConnectionClosed:
                    pass

            tasks = [
                asyncio.create_task(client_to_proxmox()),
                asyncio.create_task(proxmox_to_client()),
            ]
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()

    # -- Server lifecycle --

    async def run(self, interactive: bool = True) -> None:
        """Start the server.

        Args:
            interactive: If True (default), wait for Enter key or all
                connections to close.  If False (background mode), only
                wait for connections to close — no stdin reading.
        """
        async with serve(
            self.handle_vnc_proxy,
            "localhost",
            self.local_port,
            process_request=self.process_request,
            # Disable server-side ping to not interfere with VNC protocol
            ping_interval=None,
        ):
            if interactive:
                # Wait for Enter key in a background thread
                loop = asyncio.get_event_loop()
                stdin_task = asyncio.ensure_future(
                    loop.run_in_executor(None, sys.stdin.readline)
                )

                while not stdin_task.done():
                    await asyncio.sleep(1)
                    # Auto-exit when all connections close after initial use
                    if self._ever_connected and self._active_connections == 0:
                        idle = time.monotonic() - self._last_disconnect
                        if idle >= self.DISCONNECT_GRACE:
                            break

                stdin_task.cancel()
            else:
                # Background mode: wait for connections to close
                while True:
                    await asyncio.sleep(1)
                    if self._ever_connected and self._active_connections == 0:
                        idle = time.monotonic() - self._last_disconnect
                        if idle >= self.DISCONNECT_GRACE:
                            break
