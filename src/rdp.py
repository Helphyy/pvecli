"""RDP client detection, command construction, and execution."""

import os
import shutil
import subprocess
import sys

from .api.exceptions import PVECliError


def detect_rdp_client() -> tuple[str, str] | tuple[None, None]:
    """Detect available RDP client.

    Returns (client_type, path) or (None, None).
    """
    if sys.platform == "linux":
        for client in ("xfreerdp3", "xfreerdp", "rdesktop"):
            path = shutil.which(client)
            if path:
                return (client, path)
    elif sys.platform == "darwin":
        # macOS: Microsoft Remote Desktop via open
        path = shutil.which("open")
        if path:
            return ("open", path)
    elif sys.platform == "win32":
        path = shutil.which("mstsc")
        if path:
            return ("mstsc", path)
    return (None, None)


def get_install_hint() -> str:
    """Return install instructions for the current platform."""
    if sys.platform == "linux":
        return "Install a RDP client: sudo apt install freerdp3-x11"
    elif sys.platform == "darwin":
        return "Install Microsoft Remote Desktop from the App Store"
    elif sys.platform == "win32":
        return "mstsc.exe not found (should be built-in on Windows)"
    return "No RDP client available for this platform"


def build_rdp_command(
    client_type: str,
    host: str,
    port: int = 3389,
    user: str | None = None,
    password: str | None = None,
    domain: str | None = None,
    fullscreen: bool = False,
    resolution: str | None = None,
) -> list[str]:
    """Build RDP command arguments for the detected client."""
    if client_type in ("xfreerdp3", "xfreerdp"):
        args = [client_type, f"/v:{host}:{port}"]
        if user:
            args.append(f"/u:{user}")
        if password:
            args.append(f"/p:{password}")
        args.append(f"/d:{domain or ''}")
        args += ["/dynamic-resolution", "+clipboard", "/cert:ignore", "/log-level:FATAL"]
        if fullscreen:
            args.append("/f")
        if resolution:
            args.append(f"/size:{resolution}")
        return args

    if client_type == "rdesktop":
        args = ["rdesktop"]
        if user:
            args += ["-u", user]
        if fullscreen:
            args.append("-f")
        if resolution:
            args += ["-g", resolution]
        args.append(f"{host}:{port}")
        return args

    if client_type == "open":
        # macOS: open rdp:// URL
        url = f"rdp://full%20address=s:{host}:{port}"
        if user:
            url += f"&username=s:{user}"
        return ["open", url]

    if client_type == "mstsc":
        return ["mstsc", f"/v:{host}:{port}"]

    raise PVECliError(f"Unknown RDP client type: {client_type}")


def exec_rdp(args: list[str]) -> int:
    """Launch the RDP client, showing stderr only on failure."""
    proc = subprocess.Popen(args, stderr=subprocess.PIPE)
    _, stderr = proc.communicate()
    if proc.returncode != 0 and stderr:
        # Extract the last meaningful error line
        for line in reversed(stderr.decode(errors="replace").splitlines()):
            if "[ERROR]" in line or "ERRCONNECT" in line:
                # e.g. [ERROR][com.freerdp...] - some message
                msg = line.split("] - ", 1)[-1].strip() if "] - " in line else line.strip()
                raise PVECliError(f"RDP connection failed: {msg}")
        raise PVECliError("RDP connection failed (check credentials or network)")
    return proc.returncode


def create_ssh_tunnel(
    node_host: str,
    ssh_user: str,
    ssh_port: int,
    ssh_key: str | None,
    vm_ip: str,
    rdp_port: int,
    local_port: int,
) -> subprocess.Popen:
    """Create an SSH tunnel for RDP traffic.

    Runs: ssh -L local_port:vm_ip:rdp_port user@node -N
    """
    if not shutil.which("ssh"):
        raise PVECliError("ssh command not found (required for --tunnel)")

    args = ["ssh", "-L", f"{local_port}:{vm_ip}:{rdp_port}"]
    if ssh_port != 22:
        args += ["-p", str(ssh_port)]
    if ssh_key:
        args += ["-i", ssh_key]
    args += ["-N", "-o", "StrictHostKeyChecking=no", f"{ssh_user}@{node_host}"]

    return subprocess.Popen(args)
