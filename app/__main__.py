import logging
import socket

import uvicorn

from .config import load_settings


def _listen_sockets(host: str, port: int) -> list[socket.socket]:
    """For a local-only server, listen on both 127.0.0.1 and ::1: browsers may resolve
    "localhost" to either, and with WSL's mirrored networking both reach this process."""
    hosts = ["127.0.0.1", "::1"] if host in ("127.0.0.1", "localhost") else [host]
    sockets = []
    for h in hosts:
        family = socket.AF_INET6 if ":" in h else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        try:
            sock.bind((h, port))
        except OSError:
            sock.close()
            if h == "::1" and sockets:
                continue  # no IPv6 loopback here; IPv4 alone is fine
            raise
        sockets.append(sock)
    return sockets


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per ONVIF poll otherwise
    settings = load_settings()
    sockets = _listen_sockets(settings.web_host, settings.web_port)
    logging.getLogger("tapo").info("Dashboard: http://localhost:%d", settings.web_port)
    uvicorn.Server(uvicorn.Config("app.main:app", access_log=False)).run(sockets=sockets)


if __name__ == "__main__":
    main()
