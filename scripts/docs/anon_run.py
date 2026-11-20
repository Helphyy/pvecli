"""Run pvecli with every API answer anonymized before it is displayed.

Real names and addresses are swapped for the documentation ones in each
API response, and back in each request: pvecli lays out its tables with
the documentation names, so columns stay aligned. Used by screens.py:

    PVECLI_DOCS_ANON=scripts/docs/screens.local.toml python -m scripts.docs.anon_run cluster health -p lab
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx
import tomllib


def _swap(text: str, pairs: dict[str, str]) -> str:
    # Longest first: pve-lab10 before pve-lab1. A name that ends with a
    # digit never matches the start of a longer number: 10.0.0.1 is not
    # the start of 10.0.0.12
    for old in sorted(pairs, key=len, reverse=True):
        tail = r"(?!\d)" if old[-1:].isdigit() else ""
        text = re.sub(re.escape(old) + tail, lambda _m, new=pairs[old]: new, text)
    return text


class AnonTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport, to_fake: dict[str, str]) -> None:
        self.inner = inner
        self.to_fake = to_fake
        self.to_real = {v: k for k, v in to_fake.items()}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = request.content
        real = httpx.Request(
            request.method,
            _swap(str(request.url), self.to_real),
            headers=[(k, v) for k, v in request.headers.items() if k.lower() != "content-length"],
            content=_swap(body.decode(), self.to_real).encode() if body else None,
        )
        response = await self.inner.handle_async_request(real)
        raw = await response.aread()
        try:
            text = _swap(raw.decode(), self.to_fake).encode()
        except UnicodeDecodeError:
            text = raw
        headers = [(k, v) for k, v in response.headers.items()
                   if k.lower() not in ("content-length", "content-encoding", "transfer-encoding")]
        # The reason phrase carries PVE's error message: keep it, anonymized
        reason = response.extensions.get("reason_phrase", b"")
        ext = {"reason_phrase": _swap(reason.decode(errors="replace"), self.to_fake).encode()} if reason else {}
        return httpx.Response(response.status_code, headers=headers, content=text, request=request,
                              extensions=ext)

    async def aclose(self) -> None:
        await self.inner.aclose()


def install(conf: dict[str, Any]) -> None:
    to_fake: dict[str, str] = conf.get("replace", {})
    original = httpx.AsyncClient.__init__

    def init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        inner = httpx.AsyncHTTPTransport(verify=kwargs.get("verify", True))
        kwargs["transport"] = AnonTransport(inner, to_fake)
        original(self, *args, **kwargs)

    httpx.AsyncClient.__init__ = init  # type: ignore[method-assign]

    # The profile host is shown as its documentation address too
    from src.config import ConfigManager

    get_profile = ConfigManager.get_profile

    def anon_profile(self: ConfigManager, *args: Any, **kwargs: Any) -> Any:
        profile = get_profile(self, *args, **kwargs)
        profile.host = _swap(profile.host, to_fake)
        return profile

    ConfigManager.get_profile = anon_profile  # type: ignore[method-assign]

    # The lab profiles carry their documentation names (lab1 -> prod), so
    # pvecli prints and lays out those names itself. Every other profile of
    # this machine is left out: a capture cannot reach it, even by mistake.
    names = {**conf.get("profiles", {}), conf["profile"]: conf.get("profile_as", conf["profile"])}
    load = ConfigManager.load

    def docs_load(self: ConfigManager) -> Any:
        config = load(self)
        config.profiles = {names[k]: v for k, v in config.profiles.items() if k in names}
        config.default_profile = names[conf["profile"]]
        config.groups = {}
        return config

    def no_save(self: ConfigManager, config: Any) -> None:
        raise SystemExit("anon_run: the documentation run never writes the config")

    ConfigManager.load = docs_load  # type: ignore[method-assign]
    ConfigManager.save = no_save  # type: ignore[method-assign]


def main() -> None:
    conf = tomllib.loads(Path(os.environ["PVECLI_DOCS_ANON"]).read_text())
    install(conf)
    from src.cli.main import _pad_order_option, app

    app(args=_pad_order_option(sys.argv[1:]), prog_name="pvecli")


if __name__ == "__main__":
    main()
