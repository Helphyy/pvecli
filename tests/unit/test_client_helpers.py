"""Parts of src/api/client.py that decide how PVE errors are read: the
'not implemented' test used for version fallbacks, the HA placement mode
(groups or rules) and the 403 message.

Protects compatibility across PVE versions (an unknown endpoint is a
fallback, any other error is not) and the permission message, which must
name the refused call and the missing privilege.
"""

from __future__ import annotations

import httpx
import pytest

from src.api.client import ProxmoxClient, is_not_implemented
from src.api.exceptions import APIError, PermissionError, PVECliError, ResourceNotFoundError
from src.models.config import ProfileConfig
from tests.fake_api import fixture


def make_client(host: str = "h1") -> ProxmoxClient:
    return ProxmoxClient(ProfileConfig(
        host=host, verify_ssl=False,
        auth={"type": "password", "user": "ops@pve", "password": "x"},
    ))


def recorded_error(name: str) -> APIError:
    """The APIError a recorded error fixture stands for."""
    data = fixture(name)
    return APIError(data["__error__"].strip(), status_code=data["__status__"])


# ── is_not_implemented ───────────────────────────────────────────────────


def test_501_is_not_implemented():
    assert is_not_implemented(APIError("Method 'GET /cluster/ha/rules' not implemented", status_code=501))
    assert is_not_implemented(APIError("whatever", status_code=501))


def test_not_implemented_text_without_status():
    assert is_not_implemented(APIError("Method 'POST /cluster/bulk-action/guest/start' not implemented"))
    assert is_not_implemented(APIError("NOT IMPLEMENTED", status_code=500))


@pytest.mark.parametrize("err", [
    APIError("internal error", status_code=500),
    APIError("parameter verification failed", status_code=400),
    PermissionError("Permission denied: GET /cluster/ha/rules"),
    ResourceNotFoundError("resource", "/nodes/x"),
    APIError("cannot index groups: ha groups have been migrated to rules", status_code=500),
    PVECliError("Method 'GET /x' not implemented"),  # not an API answer
    ValueError("not implemented"),
])
def test_other_errors_are_not_a_missing_endpoint(err):
    assert not is_not_implemented(err)


# ── get_ha_placement_mode ────────────────────────────────────────────────


@pytest.mark.parametrize("version", ["pve-9.1", "pve-9.2"])
async def test_placement_mode_rules_after_migration(api, version):
    api.route("GET", "/cluster/ha/groups", recorded_error(f"{version}/ha_groups"))
    assert await make_client().get_ha_placement_mode() == "rules"


async def test_placement_mode_groups_when_groups_are_listed(api):
    # PVE 8, or PVE 9 before the conversion
    api.route("GET", "/cluster/ha/groups", [
        {"group": "prefer-pve1", "nodes": "pve1:2,pve2", "restricted": 0, "nofailback": 0, "type": "group"},
    ])
    assert await make_client().get_ha_placement_mode() == "groups"


async def test_placement_mode_rules_when_groups_endpoint_is_unknown(api):
    api.route("GET", "/cluster/ha/groups", api.not_implemented("GET", "/cluster/ha/groups"))
    assert await make_client().get_ha_placement_mode() == "rules"


@pytest.mark.parametrize("err", [
    APIError("internal error", status_code=500),
    PermissionError("Permission denied: GET /cluster/ha/groups: Permission check failed (/, Sys.Audit)"),
])
async def test_placement_mode_other_errors_propagate(api, err):
    # A refused or broken call must not be mistaken for 'rules'
    api.route("GET", "/cluster/ha/groups", err)
    with pytest.raises(APIError):
        await make_client().get_ha_placement_mode()


# ── _request: how a real HTTP answer becomes an exception ────────────────


def pve_response(status: int, reason: str, body: bytes = b'{"data":null}') -> httpx.Response:
    """An answer shaped like pveproxy's: the error is in the status line."""
    return httpx.Response(
        status, content=body, headers={"content-type": "application/json;charset=UTF-8"},
        extensions={"reason_phrase": reason.encode()},
    )


async def request_with(response: httpx.Response, method: str, endpoint: str) -> None:
    client = make_client()
    client._headers = {"Cookie": "PVEAuthCookie=x", "CSRFPreventionToken": "x"}
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: response))
    try:
        await client._request(method, endpoint, retry_count=1)
    finally:
        await client.close()


async def test_403_names_method_endpoint_and_privilege():
    resp = pve_response(403, "Permission check failed (/vms/100, VM.PowerMgmt)")
    with pytest.raises(PermissionError) as exc:
        await request_with(resp, "POST", "/nodes/pve1/qemu/100/status/start")
    msg = str(exc.value)
    assert "POST /nodes/pve1/qemu/100/status/start" in msg
    assert "VM.PowerMgmt" in msg and "/vms/100" in msg
    assert exc.value.status_code == 403


async def test_403_with_a_message_in_the_body():
    body = b'{"data":null,"message":"Permission check failed (/vms/100, VM.PowerMgmt)\\n"}'
    resp = pve_response(403, "Forbidden", body)
    with pytest.raises(PermissionError) as exc:
        await request_with(resp, "POST", "/nodes/pve1/qemu/100/status/start")
    msg = str(exc.value)
    assert "POST /nodes/pve1/qemu/100/status/start" in msg and "VM.PowerMgmt" in msg


async def test_403_without_detail_still_names_the_call():
    resp = httpx.Response(403, content=b'{"data":null}')
    with pytest.raises(PermissionError) as exc:
        await request_with(resp, "GET", "cluster/ha/rules")
    assert "GET /cluster/ha/rules" in str(exc.value)
    assert not str(exc.value).endswith(": Forbidden")


async def test_501_through_request_is_not_implemented():
    resp = pve_response(501, "Method 'GET /cluster/ha/rules' not implemented")
    with pytest.raises(APIError) as exc:
        await request_with(resp, "GET", "/cluster/ha/rules")
    assert exc.value.status_code == 501
    assert is_not_implemented(exc.value)


async def test_migrated_groups_error_through_request_gives_rules():
    data = fixture("pve-9.2/ha_groups")
    resp = pve_response(data["__status__"], data["__error__"].strip())
    client = make_client()
    client._headers = {"Cookie": "PVEAuthCookie=x", "CSRFPreventionToken": "x"}
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: resp))
    try:
        assert await client.get_ha_placement_mode() == "rules"
    finally:
        await client.close()


async def test_parameter_errors_are_read_from_the_body():
    body = b'{"data":null,"errors":{"vmid":"invalid format - value does not look like a valid VM ID"}}'
    resp = pve_response(400, "Parameter verification failed.", body)
    with pytest.raises(APIError) as exc:
        await request_with(resp, "POST", "/nodes/pve1/qemu")
    assert exc.value.status_code == 400
    assert "valid VM ID" in str(exc.value)
