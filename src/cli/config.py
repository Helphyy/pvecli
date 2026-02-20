"""Configuration management commands for pvecli."""

from getpass import getpass

import typer
from rich.panel import Panel
from rich.table import Table

from ..api.client import ProxmoxClient
from ..api.exceptions import PVECliError
from ..config import AuthConfig, ConfigManager, ProfileConfig
from ..utils import (
    confirm,
    console,
    print_cancelled,
    print_error,
    print_info,
    print_success,
    prompt,
)
from ..utils.helpers import async_to_sync
from ..utils.menu import multi_select_menu, select_menu
from ..utils.network import resolve_node_host

app = typer.Typer(help="Manage pvecli configuration", no_args_is_help=True)


# ── Shared helpers ───────────────────────────────────────────────────────


def _pick_profile(config_manager: ConfigManager) -> str | None:
    """Interactive single-select for a profile. Returns profile name or None."""
    try:
        config = config_manager.get()
    except PVECliError:
        print_info("No configuration found. Run 'pvecli config add' first.")
        return None

    if not config.profiles:
        print_info("No profiles configured. Run 'pvecli config add' to create one.")
        return None

    names = sorted(config.profiles.keys())
    idx = select_menu(names, "  Select profile:")
    if idx is None:
        print_cancelled()
        return None
    return names[idx]


def _check_profile_exists(config_manager: ConfigManager, name: str) -> None:
    """Raise typer.Exit if profile already exists."""
    if config_manager.exists():
        try:
            config = config_manager.get()
            if name in config.profiles:
                print_error(f"Profile '{name}' already exists. Use 'pvecli config edit' to modify it.")
                raise typer.Exit(1)
        except PVECliError:
            pass


def _collect_profile_values(
    config_manager: ConfigManager,
    profile_name: str | None,
    host: str | None,
    port: int | None,
    user: str | None,
    token_name: str | None,
    token_value: str | None,
    password: str | None,
    verify_ssl: bool,
) -> tuple[dict, bool]:
    """Collect profile values interactively if not provided."""
    config_values = {
        "name": profile_name,
        "host": host,
        "port": port,
        "user": user,
        "token_name": token_name,
        "token_value": token_value,
        "password": password,
    }

    has_auth = (token_name and token_value) or password
    needs_interactive = any(v is None for v in [profile_name, host, port, user]) or not has_auth

    if needs_interactive:
        console.print("\n[bold cyan]═══ Profile Setup ═══[/bold cyan]\n")

        if config_values["name"] is None:
            config_values["name"] = prompt("Profile name", default="default")

        _check_profile_exists(config_manager, config_values["name"])

        if config_values["host"] is None:
            while not (val := prompt("Proxmox host (IP or hostname)")):
                print_error("Host is required")
            config_values["host"] = val

        if config_values["port"] is None:
            while True:
                try:
                    config_values["port"] = int(prompt("Proxmox port", default="8006"))
                    break
                except ValueError:
                    print_error("Port must be a valid integer")

        if config_values["user"] is None:
            config_values["user"] = prompt("Username", default="root@pam")

        if not has_auth:
            auth_methods = ["API Token (recommended)", "Password"]
            auth_idx = select_menu(auth_methods, "  Authentication method:")
            if auth_idx is None:
                print_cancelled()
                raise typer.Exit()

            if auth_idx == 0:
                if config_values["token_name"] is None:
                    while not (val := prompt("Token name")):
                        print_error("Token name is required")
                    config_values["token_name"] = val
                if config_values["token_value"] is None:
                    while not (val := prompt("Token value (UUID)")):
                        print_error("Token value is required")
                    config_values["token_value"] = val
            else:
                if config_values["password"] is None:
                    while not (val := getpass("Password: ")):
                        print_error("Password is required")
                    config_values["password"] = val

        if not verify_ssl:
            verify_ssl = confirm("Verify SSL certificate", default=False)

        if config_values.get("ssh_user") is None:
            ssh_u = prompt("Default SSH user", default="root")
            config_values["ssh_user"] = ssh_u if ssh_u != "root" else None

        if config_values.get("rdp_user") is None:
            rdp_u = prompt("Default RDP user", default="Administrator")
            config_values["rdp_user"] = rdp_u if rdp_u != "Administrator" else None

    return config_values, verify_ssl


def _validate_and_create_profile(
    config_values: dict,
    verify_ssl: bool,
) -> tuple[ProfileConfig, AuthConfig]:
    """Validate config values and create profile objects."""
    has_token = config_values.get("token_name") and config_values.get("token_value")
    has_password = config_values.get("password")

    if not has_token and not has_password:
        print_error("Either token or password authentication required")
        raise typer.Exit(1)

    required_fields = ["name", "host", "port", "user"]
    if has_token:
        required_fields.extend(["token_name", "token_value"])
    else:
        required_fields.append("password")

    missing = [k for k in required_fields if config_values.get(k) is None]
    if missing:
        print_error(f"Missing required values: {', '.join(missing)}")
        raise typer.Exit(1)

    if has_token:
        auth_config = AuthConfig(
            type="token",
            user=config_values["user"],
            token_name=config_values["token_name"],
            token_value=config_values["token_value"],
        )
    else:
        auth_config = AuthConfig(
            type="password",
            user=config_values["user"],
            password=config_values["password"],
        )

    profile = ProfileConfig(
        host=config_values["host"],
        port=int(config_values["port"]) if isinstance(config_values["port"], str) else config_values["port"],
        verify_ssl=verify_ssl,
        auth=auth_config,
        ssh_user=config_values.get("ssh_user"),
        rdp_user=config_values.get("rdp_user"),
    )

    return profile, auth_config


def _render_profile_panel(name: str, profile: ProfileConfig, is_default: bool = False) -> Panel:
    """Build a Rich Panel for a profile."""
    lines = []
    lines.append("[bold]── Connection ──[/bold]")
    lines.append(f"[bold]Host:[/bold]        {profile.host}:{profile.port}")
    lines.append(f"[bold]User:[/bold]        {profile.auth.user}")
    lines.append(f"[bold]Auth:[/bold]        {profile.auth.type}")
    lines.append(f"[bold]SSL:[/bold]         {'Yes' if profile.verify_ssl else 'No'}")

    lines.append("")
    lines.append("[bold]── Defaults ──[/bold]")
    lines.append(f"[bold]SSH user:[/bold]    {profile.ssh_user or 'root'}")
    lines.append(f"[bold]RDP user:[/bold]    {profile.rdp_user or 'Administrator'}")
    lines.append(f"[bold]Timeout:[/bold]     {profile.timeout}s")

    if is_default:
        lines.append("")
        lines.append("[green]Default profile[/green]")

    return Panel("\n".join(lines), title=f"Profile: {name}", border_style="blue")


# ── config add ───────────────────────────────────────────────────────────


@app.command("add")
def add_profile(
    name: str = typer.Argument(None, help="Profile name"),
    host: str = typer.Option(None, "--host", "-ho", help="Proxmox host (IP or hostname)"),
    user: str = typer.Option(None, "--user", "-us", help="Username (e.g., root@pam)"),
    token_name: str = typer.Option(None, "--token-name", "-tn", help="API token name"),
    token_value: str = typer.Option(None, "--token-value", "-tv", help="API token value (UUID)"),
    password: str = typer.Option(None, "--password", "-pw", help="Password (not recommended)"),
    port: int = typer.Option(None, "--port", "-po", help="Proxmox port"),
    verify_ssl: bool = typer.Option(False, "--verify-ssl", "-vs", is_flag=True, help="Verify SSL certificate"),
) -> None:
    """Add a new profile."""
    config_manager = ConfigManager()

    try:
        # Check for duplicate early if name provided as argument
        if name is not None:
            _check_profile_exists(config_manager, name)

        config_values, verify_ssl = _collect_profile_values(
            config_manager, name, host, port, user, token_name, token_value, password, verify_ssl,
        )

        profile, _ = _validate_and_create_profile(config_values, verify_ssl)

        console.print()
        console.print(_render_profile_panel(config_values["name"], profile))

        if not confirm("\nSave this profile?", default=True):
            print_cancelled()
            raise typer.Exit()

        # Check if first profile before adding (add_profile auto-sets default for first)
        is_first = not config_manager.exists() or not config_manager.get().profiles

        config_manager.add_profile(config_values["name"], profile)

        if is_first:
            print_success(f"Profile '{config_values['name']}' added (set as default)")
        elif confirm("Set as default profile?", default=False):
            config_manager.set_default_profile(config_values["name"])
            print_success(f"Profile '{config_values['name']}' added (set as default)")
        else:
            print_success(f"Profile '{config_values['name']}' added")

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


# ── config remove ────────────────────────────────────────────────────────


@app.command("remove")
def remove_profile(
    name: str = typer.Argument(None, help="Profile name"),
    all_profiles: bool = typer.Option(False, "--all", "-al", is_flag=True, help="Remove all profiles"),
) -> None:
    """Remove a profile or all profiles."""
    config_manager = ConfigManager()

    try:
        if all_profiles:
            config = config_manager.get()
            if not config.profiles:
                print_info("No profiles to remove")
                return

            if not confirm("Remove ALL profiles?", default=False):
                print_cancelled()
                return

            profile_count = len(config.profiles)
            for profile_name in list(config.profiles.keys()):
                config_manager.remove_profile(profile_name)

            print_success(f"Removed {profile_count} profile(s)")
        else:
            if not name:
                config = config_manager.get()
                if not config.profiles:
                    print_info("No profiles configured. Run 'pvecli config add' to create one.")
                    return

                names = sorted(config.profiles.keys())
                sel = multi_select_menu(names, "  Profiles to remove (Space to toggle, Enter to confirm):")
                if sel is None:
                    print_cancelled()
                    return
                selected = [names[i] for i in sel]
                if not selected:
                    print_cancelled()
                    return
            else:
                selected = [name]

            label = ", ".join(f"'{n}'" for n in selected)
            if not confirm(f"Remove profile(s) {label}?", default=False):
                print_cancelled()
                return

            for n in selected:
                config_manager.remove_profile(n)

            if len(selected) == 1:
                print_success(f"Profile '{selected[0]}' removed")
            else:
                print_success(f"{len(selected)} profiles removed: {', '.join(selected)}")

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


# ── config edit ──────────────────────────────────────────────────────────


@app.command("edit")
def edit_profile(
    name: str = typer.Argument(None, help="Profile name"),
) -> None:
    """Interactively edit a profile."""
    config_manager = ConfigManager()

    try:
        if not name:
            name = _pick_profile(config_manager)
            if name is None:
                return

        current = config_manager.get_profile(name)

        console.print("\n[bold cyan]═══ Edit Profile ═══[/bold cyan]\n")

        # (key, label, current_value)
        fields = [
            ("name", "Profile name", name),
            ("host", "Host", current.host),
            ("port", "Port", current.port),
            ("user", "User", current.auth.user),
            ("auth_type", "Auth type", current.auth.type),
            ("verify_ssl", "Verify SSL", current.verify_ssl),
            ("ssh_user", "SSH user", current.ssh_user or "root"),
            ("ssh_port", "SSH port", current.ssh_port),
            ("rdp_user", "RDP user", current.rdp_user or "Administrator"),
            ("rdp_port", "RDP port", current.rdp_port),
        ]

        changes: dict = {}
        max_label = max(len(f[1]) for f in fields)

        while True:
            options = []
            for key, label, original in fields:
                current_val = changes.get(key, original)
                if isinstance(current_val, bool):
                    display = "Yes" if current_val else "No"
                else:
                    display = str(current_val)
                prefix = "* " if key in changes else "  "
                options.append(f"{prefix}{label.ljust(max_label)}  {display}")

            # Auth-specific fields
            auth_type = changes.get("auth_type", current.auth.type)
            pw_menu_idx = -1
            token_name_idx = -1
            token_value_idx = -1

            if auth_type == "password":
                pw_prefix = "* " if "password" in changes else "  "
                pw_display = "(changed)" if "password" in changes else "(unchanged)"
                options.append(f"{pw_prefix}{'Password'.ljust(max_label)}  {pw_display}")
                pw_menu_idx = len(options) - 1
            else:
                tn = changes.get("token_name", current.auth.token_name or "")
                tv = changes.get("token_value", current.auth.token_value or "")
                tn_prefix = "* " if "token_name" in changes else "  "
                tv_prefix = "* " if "token_value" in changes else "  "
                options.append(f"{tn_prefix}{'Token name'.ljust(max_label)}  {tn}")
                token_name_idx = len(options) - 1
                options.append(f"{tv_prefix}{'Token value'.ljust(max_label)}  {tv[:8]}..." if len(tv) > 8 else f"{tv_prefix}{'Token value'.ljust(max_label)}  {tv}")
                token_value_idx = len(options) - 1

            # Separator + Apply / Cancel
            options.append("  " + "─" * (max_label + 20))
            total = len(changes)
            options.append(f"  Apply {total} change(s)" if total else "  (no changes)")
            apply_idx = len(options) - 1
            options.append("  Cancel")

            selected = select_menu(options, f"\n  Profile: {changes.get('name', name)}")

            if selected is None or selected == len(options) - 1:
                print_cancelled()
                return

            if selected == apply_idx and total:
                break

            if selected == pw_menu_idx:
                pw = getpass("New password: ")
                if pw:
                    changes["password"] = pw
                    # Switching to password auth
                    if changes.get("auth_type", current.auth.type) == "token":
                        changes["auth_type"] = "password"
                        changes.pop("token_name", None)
                        changes.pop("token_value", None)
                continue

            if selected == token_name_idx:
                val = prompt("  Token name", default=changes.get("token_name", current.auth.token_name or ""))
                if val != (current.auth.token_name or ""):
                    changes["token_name"] = val
                elif "token_name" in changes:
                    del changes["token_name"]
                continue

            if selected == token_value_idx:
                val = prompt("  Token value", default=changes.get("token_value", current.auth.token_value or ""))
                if val != (current.auth.token_value or ""):
                    changes["token_value"] = val
                elif "token_value" in changes:
                    del changes["token_value"]
                continue

            # Simple fields
            if selected < len(fields):
                key, label, original = fields[selected]

                if key == "auth_type":
                    idx = select_menu(["token", "password"], f"  {label}:")
                    if idx is not None:
                        new_val = ["token", "password"][idx]
                        if new_val != current.auth.type:
                            changes[key] = new_val
                        elif key in changes:
                            del changes[key]
                elif key == "verify_ssl":
                    idx = select_menu(["Yes", "No"], f"  {label}:")
                    if idx is not None:
                        new_val = idx == 0
                        if new_val != original:
                            changes[key] = new_val
                        elif key in changes:
                            del changes[key]
                elif isinstance(original, int):
                    raw = prompt(f"  {label}", default=str(changes.get(key, original)))
                    try:
                        new_val = int(raw)
                        if new_val != original:
                            changes[key] = new_val
                        elif key in changes:
                            del changes[key]
                    except ValueError:
                        print_error("Invalid number")
                else:
                    new_val = prompt(f"  {label}", default=str(changes.get(key, original)))
                    if new_val != str(original):
                        changes[key] = new_val
                    elif key in changes:
                        del changes[key]

        # Summary
        console.print("\n[bold]Changes:[/bold]")
        for key, label, original in fields:
            if key in changes:
                if isinstance(original, bool):
                    console.print(f"  {label}: {'Yes' if original else 'No'} -> {'Yes' if changes[key] else 'No'}")
                else:
                    console.print(f"  {label}: {original} -> {changes[key]}")
        if "password" in changes:
            console.print("  Password: (will be changed)")
        if "token_name" in changes:
            console.print(f"  Token name: {current.auth.token_name or ''} -> {changes['token_name']}")
        if "token_value" in changes:
            console.print(f"  Token value: (will be changed)")

        if not confirm("Apply these changes?"):
            print_cancelled()
            return

        # Build updated profile
        final_name = changes.get("name", name)
        auth_type = changes.get("auth_type", current.auth.type)

        if auth_type == "token":
            auth_config = AuthConfig(
                type="token",
                user=changes.get("user", current.auth.user),
                token_name=changes.get("token_name", current.auth.token_name),
                token_value=changes.get("token_value", current.auth.token_value),
            )
        else:
            auth_config = AuthConfig(
                type="password",
                user=changes.get("user", current.auth.user),
                password=changes.get("password", current.auth.password),
            )

        profile = ProfileConfig(
            host=changes.get("host", current.host),
            port=changes.get("port", current.port),
            verify_ssl=changes.get("verify_ssl", current.verify_ssl),
            auth=auth_config,
            ssh_user=changes.get("ssh_user", current.ssh_user) if changes.get("ssh_user", current.ssh_user or "root") != "root" else None,
            ssh_port=changes.get("ssh_port", current.ssh_port),
            ssh_key=current.ssh_key,
            rdp_user=changes.get("rdp_user", current.rdp_user) if changes.get("rdp_user", current.rdp_user or "Administrator") != "Administrator" else None,
            rdp_port=changes.get("rdp_port", current.rdp_port),
        )

        # If renamed, remove old profile
        if final_name != name:
            config_manager.remove_profile(name)

        config_manager.add_profile(final_name, profile)
        print_success(f"Profile '{final_name}' updated successfully")

    except KeyboardInterrupt:
        console.print()
        print_cancelled()
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


# ── config default ───────────────────────────────────────────────────────


@app.command("default")
def set_default(
    name: str = typer.Argument(None, help="Profile name"),
) -> None:
    """Set the default profile."""
    config_manager = ConfigManager()

    try:
        if not name:
            name = _pick_profile(config_manager)
            if name is None:
                return

        config_manager.set_default_profile(name)
        print_success(f"Default profile set to '{name}'")

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


# ── config list ──────────────────────────────────────────────────────────


@app.command("list")
def list_profiles() -> None:
    """List all profiles."""
    config_manager = ConfigManager()

    try:
        config = config_manager.get()
        if not config.profiles:
            print_info("No profiles configured. Run 'pvecli config add' to create one.")
            return

        table = Table(title="Configured Profiles", show_header=True, header_style="bold cyan")
        table.add_column("Profile", style="cyan")
        table.add_column("Host")
        table.add_column("User")
        table.add_column("Auth Type")
        table.add_column("SSH user")
        table.add_column("RDP user")
        table.add_column("Default", style="green")

        for profile_name, profile in config.profiles.items():
            is_default = "\u2713" if profile_name == config.default_profile else ""
            table.add_row(
                profile_name,
                f"{profile.host}:{profile.port}",
                profile.auth.user,
                profile.auth.type,
                profile.ssh_user or "root",
                profile.rdp_user or "Administrator",
                is_default,
            )

        console.print(table)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


# ── config show ──────────────────────────────────────────────────────────


@app.command("show")
def show_profile(
    name: str = typer.Argument(None, help="Profile name"),
    all_profiles: bool = typer.Option(False, "--all", "-a", is_flag=True, help="Show all profiles"),
) -> None:
    """Show profile details."""
    config_manager = ConfigManager()

    try:
        config = config_manager.get()

        if all_profiles:
            if not config.profiles:
                print_info("No profiles configured")
                return
            for pname in sorted(config.profiles):
                profile = config.profiles[pname]
                is_default = pname == config.default_profile
                console.print(_render_profile_panel(pname, profile, is_default))
        else:
            if not name:
                name = _pick_profile(config_manager)
                if name is None:
                    return

            profile = config_manager.get_profile(name)
            is_default = name == config.default_profile
            console.print(_render_profile_panel(name, profile, is_default))

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)


# ── config test ──────────────────────────────────────────────────────────


@app.command("test")
@async_to_sync
async def test_profile(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to test"),
) -> None:
    """Test connection to Proxmox."""
    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)
        profile_name = profile or config_manager.get().default_profile

        print_info(f"Testing connection to {profile_config.host}:{profile_config.port}...")

        async with ProxmoxClient(profile_config) as client:
            version = await client.get_version()
            print_success(f"Connection successful to '{profile_name}'")
            print_info(f"Proxmox VE version: {version.get('version', 'unknown')}")
            print_info(f"API version: {version.get('release', 'unknown')}")

    except PVECliError as e:
        print_error(f"Connection failed: {e}")
        raise typer.Exit(1)


# ── config login ─────────────────────────────────────────────────────────


@app.command("login")
def login_web(
    profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
) -> None:
    """Open Proxmox web interface login page in browser."""
    from ..utils import open_browser_window

    config_manager = ConfigManager()

    try:
        profile_config = config_manager.get_profile(profile)
        host = resolve_node_host(profile_config)
        login_url = f"https://{host}:{profile_config.port}/"

        print_success("Opening Proxmox web interface...")
        open_browser_window(login_url)

    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
