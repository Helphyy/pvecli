"""Background VNC proxy server entry point.

Usage: python -m src.vnc '{"proxmox_host": ..., ...}'
"""

import asyncio
import json
import sys

from .server import VNCProxyServer


def main() -> None:
    config = json.loads(sys.argv[1])
    server = VNCProxyServer(**config)
    asyncio.run(server.run(interactive=False))


if __name__ == "__main__":
    main()
