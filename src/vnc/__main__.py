"""Background VNC proxy server entry point.

The server configuration, which carries the Proxmox session cookie and the
VNC credentials, is read as JSON from stdin. It must never be passed as an
argument: any local user can read a process command line in /proc.
"""

import asyncio
import json
import sys

from .server import VNCProxyServer


def main() -> None:
    config = json.loads(sys.stdin.read())
    server = VNCProxyServer(**config)
    asyncio.run(server.run(interactive=False))


if __name__ == "__main__":
    main()
