"""Screenshots of the Proxmox VE web interface, for the pages where a reader
may do in the browser what pvecli needs first (create its user and token).

Only the dialog is captured: no tree of the cluster, no logo, no lab name.
The lab is the profile of scripts/docs/screens.local.toml, which must log in
with a password (the web interface takes no API token). The user and token
created for the shots are removed at the end.

    python -m scripts.docs.webui

Needs the `docs` extra and `python -m playwright install chromium`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import tomllib

from src.config import ConfigManager

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "assets" / "images"
LOCAL = ROOT / "scripts" / "docs" / "screens.local.toml"

USER, REALM, TOKEN = "pvecli", "pve", "cli"
USERID = f"{USER}@{REALM}"
# Shown instead of the real secret
DOC_SECRET = "3f6b2c1e-8a4d-4f7e-9b2a-5c8d7e6f1a09"


def _api(base: str, ticket: str, csrf: str) -> httpx.Client:
    return httpx.Client(
        base_url=base + "/api2/json",
        verify=False,
        cookies={"PVEAuthCookie": ticket},
        headers={"CSRFPreventionToken": csrf},
    )


def _cleanup(api: httpx.Client) -> None:
    # Removing the user removes its tokens and its permissions
    api.delete(f"/access/users/{USERID}")


async def _shots(base: str, user: str, password: str) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(
            ignore_https_errors=True, viewport={"width": 1400, "height": 900}, device_scale_factor=2
        )
        page = await ctx.new_page()
        await page.goto(base + "/", wait_until="networkidle")
        await page.wait_for_timeout(1500)
        # The login form: the realm is taken from the user name (root@pam)
        name = user.partition("@")[0]
        await page.locator("input[name=username]").fill(name)
        await page.locator("input[name=password]").fill(password)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(4000)
        # The "No valid subscription" notice of a lab without one
        notice = page.locator(".x-message-box:visible .x-btn-inner", has_text="OK")
        if await notice.count():
            await notice.first.click()
            await page.wait_for_timeout(800)

        async def menu(text: str) -> None:
            await page.locator(".x-treelist-item-text", has_text=text).first.click()
            await page.wait_for_timeout(800)

        async def add(label: str | None = None) -> None:
            await page.locator(".x-panel:visible .x-btn-inner", has_text="Add").first.click()
            await page.wait_for_timeout(500)
            if label:
                await page.locator(".x-menu-item-text:visible", has_text=label).first.click()
            await page.wait_for_timeout(1200)

        async def field(label: str, value: str) -> None:
            box = page.locator(".x-window:visible .x-field", has=page.locator("label", has_text=label)).first
            inp = box.locator("input").first
            await inp.click()
            # A read-only combobox opens its list on click; the others take text
            if await inp.is_editable():
                await inp.fill(value)
            await page.wait_for_timeout(700)
            # A combobox opens a floating list: take the row with this exact text
            if await box.locator(".x-form-trigger").count():
                await page.locator(".x-layer:visible").get_by_text(value, exact=True).first.click()
            await page.keyboard.press("Tab")
            await page.wait_for_timeout(400)
            current = await inp.input_value()
            if value not in current and not value.startswith(current.rstrip(".")):
                raise SystemExit(f"{label}: got {current!r}, wanted {value!r}")

        async def untick(label: str) -> None:
            box = page.locator(".x-window:visible .x-form-type-checkbox",
                               has=page.locator("label", has_text=label)).first
            if "x-form-cb-checked" in (await box.get_attribute("class") or ""):
                await box.locator(".x-form-cb-input").click()
                await page.wait_for_timeout(300)
            if "x-form-cb-checked" in (await box.get_attribute("class") or ""):
                raise SystemExit(f"{label} is still ticked")

        async def shot(name: str) -> None:
            await page.locator(".x-window:visible").last.screenshot(path=str(OUT / f"{name}.png"))
            print(f"{name}.png")

        async def ok(text: str) -> None:
            await page.locator(".x-window:visible .x-btn-inner", has_text=text).last.click()
            await page.wait_for_timeout(1500)

        await page.locator(".x-tree-node-text", has_text="Datacenter").first.click()
        await page.wait_for_timeout(1000)

        # The user
        await menu("Users")
        await add()
        await field("User name", USER)
        await field("Realm", "Proxmox VE authentication server")
        await shot("webui-user-add")
        await ok("Add")

        # Its permission on /
        await menu("Permissions")
        await add("User Permission")
        await field("Path", "/")
        await field("User", USERID)
        await field("Role", "Administrator")
        await shot("webui-permission-add")
        await ok("Add")

        # Its token, without privilege separation
        await menu("API Tokens")
        await add()
        await field("User", USERID)
        await field("Token ID", TOKEN)
        await untick("Privilege Separation")
        await shot("webui-token-add")
        await ok("Add")
        # The secret, shown once: replaced before the shot
        await page.evaluate(
            """(doc) => document.querySelectorAll('.x-window input').forEach(i => {
                 if (/^[0-9a-f-]{36}$/.test(i.value)) i.value = doc;
               })""",
            DOC_SECRET,
        )
        await shot("webui-token-secret")
        await browser.close()


def main() -> None:
    local = tomllib.loads(LOCAL.read_text())
    profile = ConfigManager().get_profile(local["profile"])
    if profile.auth.type != "password" or not profile.auth.password:
        raise SystemExit(f"Profile {local['profile']} must log in with a password for the web interface")
    base = f"https://{profile.host}:{profile.port}"
    r = httpx.post(base + "/api2/json/access/ticket", verify=False,
                   data={"username": profile.auth.user, "password": profile.auth.password})
    r.raise_for_status()
    data = r.json()["data"]
    api = _api(base, data["ticket"], data["CSRFPreventionToken"])
    _cleanup(api)
    try:
        asyncio.run(_shots(base, profile.auth.user, profile.auth.password))
    finally:
        _cleanup(api)


if __name__ == "__main__":
    main()
