"""A fake Proxmox API behind the real ProxmoxClient.

Only the network layer of the client is replaced (connect and _request):
every real client method runs, so a test exercises the same code as
production. Responses are routed by HTTP method and path, per profile host,
so several clusters can be simulated at once.

    api = FakeAPI()
    api.route("GET", "/cluster/resources", [{"vmid": 100, ...}])
    api.route("POST", "/nodes/{node}/qemu/{vmid}/status/start", "UPID:pve1:...")
    api.route("GET", "/cluster/ha/groups", api.error(500, "ha groups have been migrated to rules"))
    api.route("POST", "/cluster/ha/status/disarm-ha", api.not_implemented())
    api.calls  # [(host, method, path, params, data), ...]

A route value can be a plain object (returned), an exception instance
(raised), or a callable(call) returning a value or raising.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.api.exceptions import APIError, PermissionError, ResourceNotFoundError

FIXTURES = Path(__file__).parent / "fixtures"
DEFAULT_HOST = "*"


@dataclass
class Call:
    host: str
    method: str
    path: str
    params: dict[str, Any] | None
    data: dict[str, Any] | None
    match: dict[str, str] = field(default_factory=dict)


def _pattern(path: str) -> re.Pattern[str]:
    """'/nodes/{node}/qemu/{vmid}' -> regex with named groups."""
    regex = re.sub(r"\\{(\w+)\\}", r"(?P<\1>[^/]+)", re.escape(path.rstrip("/")))
    return re.compile(f"^{regex}$")


def fixture(name: str) -> Any:
    """A recorded API response, e.g. fixture('pve-9.1/cluster_status')."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


def upid(node: str = "pve1", kind: str = "qmstart", vmid: int | str = "", user: str = "root@pam") -> str:
    return f"UPID:{node}:0000AAAA:0000BBBB:6AFD0000:{kind}:{vmid}:{user}:"


class FakeAPI:
    def __init__(self) -> None:
        self.routes: list[tuple[str, str, re.Pattern[str], Any]] = []
        self.calls: list[Call] = []
        # Tasks finish at once unless a test routes their status itself
        self.route("GET", "/nodes/{node}/tasks/{upid}/status", {"status": "stopped", "exitstatus": "OK"})

    # ── route definition ──────────────────────────────────────────────

    def route(self, method: str, path: str, value: Any, host: str = DEFAULT_HOST) -> FakeAPI:
        """Register a response. The last registration of a path wins."""
        self.routes.insert(0, (host, method.upper(), _pattern(path), value))
        return self

    @staticmethod
    def error(status: int, message: str) -> APIError:
        return APIError(message, status_code=status)

    @staticmethod
    def not_implemented(method: str = "GET", path: str = "/x") -> APIError:
        # What PVE answers for an endpoint it does not know
        return APIError(f"Method '{method} {path}' not implemented", status_code=501)

    @staticmethod
    def forbidden(path: str = "/", privilege: str = "Sys.Modify") -> PermissionError:
        return PermissionError(f"Permission denied: Permission check failed ({path}, {privilege})")

    # ── what the patched client calls ─────────────────────────────────

    def handle(self, host: str, method: str, endpoint: str, params: Any, data: Any) -> Any:
        path = "/" + endpoint.lstrip("/").rstrip("/")
        for route_host, route_method, pattern, value in self.routes:
            if route_method != method or route_host not in (host, DEFAULT_HOST):
                continue
            m = pattern.match(path)
            if not m:
                continue
            call = Call(host, method, path, params, data, m.groupdict())
            self.calls.append(call)
            if callable(value) and not isinstance(value, type):
                value = value(call)
            if isinstance(value, BaseException):
                raise value
            return copy.deepcopy(value)
        self.calls.append(Call(host, method, path, params, data))
        raise ResourceNotFoundError("resource", f"{method} {path} (no fake route)")

    # ── assertions helpers ────────────────────────────────────────────

    def called(self, method: str, path: str) -> list[Call]:
        pattern = _pattern(path)
        return [c for c in self.calls if c.method == method.upper() and pattern.match(c.path)]

    def writes(self) -> list[Call]:
        """Every call that changes something (anything but GET)."""
        return [c for c in self.calls if c.method != "GET"]


def install(monkeypatch: Any, api: FakeAPI) -> None:
    """Route every ProxmoxClient through the fake API."""
    from src.api.client import ProxmoxClient

    async def connect(self: ProxmoxClient) -> None:
        self._headers = {"Cookie": "PVEAuthCookie=fake", "CSRFPreventionToken": "fake"}
        self._client = object()  # type: ignore[assignment]

    async def close(self: ProxmoxClient) -> None:
        self._client = None

    async def request(self: ProxmoxClient, method: str, endpoint: str, params: Any = None,
                      data: Any = None, retry_count: int = 3) -> Any:
        return api.handle(self.profile.host, method, endpoint, params, data)

    monkeypatch.setattr(ProxmoxClient, "connect", connect)
    monkeypatch.setattr(ProxmoxClient, "close", close)
    monkeypatch.setattr(ProxmoxClient, "_request", request)


Responder = Callable[[Call], Any]
