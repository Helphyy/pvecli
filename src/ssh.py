"""SSH command construction and execution."""

import os
import shutil

from .api.exceptions import PVECliError


def build_ssh_command(
    host: str,
    user: str,
    port: int = 22,
    key: str | None = None,
    jump: str | None = None,
    command: str | None = None,
) -> list[str]:
    """Build an SSH command argument list."""
    args = ["ssh"]
    if port != 22:
        args += ["-p", str(port)]
    if key:
        args += ["-i", key]
    if jump:
        args += ["-J", jump]
    args.append(f"{user}@{host}")
    if command:
        args.append(command)
    return args


def exec_ssh(args: list[str]) -> None:
    """Replace the current process with SSH."""
    if not shutil.which("ssh"):
        raise PVECliError("ssh command not found")
    os.execvp("ssh", args)
