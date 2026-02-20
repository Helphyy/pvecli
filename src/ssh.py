"""SSH command construction and execution."""

import shutil
import subprocess
import sys

from .api.exceptions import PVECliError
from .utils.output import print_error, print_warning


def build_ssh_command(
    host: str,
    user: str,
    port: int = 22,
    key: str | None = None,
    jump: str | None = None,
    command: str | None = None,
) -> list[str]:
    """Build an SSH command argument list."""
    args = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
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
    """Run SSH and handle common errors."""
    if not shutil.which("ssh"):
        raise PVECliError("ssh command not found")

    result = subprocess.run(args)

    if result.returncode == 255:
        print_error(
            "SSH connection failed. Common causes:\n"
            "  - Host unreachable or connection refused\n"
            "  - Authentication failed"
        )
        sys.exit(255)
    elif result.returncode != 0:
        sys.exit(result.returncode)
