"""Entry point: `python -m wft [--env prod|uat]`."""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import webbrowser

import uvicorn

from wft.config import ENVIRONMENTS, ConfigError, check_share_marker, load_config
from wft.definitions import DefinitionRegistry
from wft.readmodel import ReadModel
from wft.repository import FileRepository, InstanceLock, InstanceLockError, default_identity
from wft.services import Services
from wft.web import security
from wft.web.app import create_app

HOST = "127.0.0.1"


def bind_socket(preferred_port: int) -> socket.socket:
    """Bind to the preferred port on 127.0.0.1, or any free port if it's taken."""
    for port in (preferred_port, 0):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Windows: stop another user on a shared host binding the same port over ours.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind((HOST, port))
            return sock
        except OSError:
            sock.close()
    raise OSError("No free port available")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wft", description="Workflow tracker")
    parser.add_argument("--env", choices=ENVIRONMENTS, default="prod")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.env)
        if not config.share_root.is_dir():
            raise ConfigError(f"share root not reachable: {config.share_root}")
        check_share_marker(config)
    except ConfigError as e:
        print(f"WFT: {e}", file=sys.stderr)
        return 2

    user, host = default_identity()
    lock = InstanceLock(config.share_root / "instance-locks" / f"{user}.lock", user, host)
    try:
        lock.acquire()
    except InstanceLockError as e:
        print(f"WFT: {e}", file=sys.stderr)
        return 3

    try:
        sock = bind_socket(config.port)
        port = sock.getsockname()[1]
        token = security.new_token()

        registry = DefinitionRegistry.load(config.definitions_dir)
        read_model = ReadModel(config.cache_path, registry)
        services = Services(FileRepository(config.share_root, user), read_model, registry, user, host)
        services.sync()
        app = create_app(services, config.env, token, port)

        url = f"http://{HOST}:{port}{security.AUTH_PATH}?token={token}"
        if port != config.port:
            print(f"WFT: port {config.port} is in use; using {port} instead.")
        print(f"WFT ({config.env}) running as {user}. Open: {url}\nPress Ctrl+C to stop.")
        if not args.no_browser:
            threading.Timer(1.0, webbrowser.open, args=(url,)).start()
        uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[sock])
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
