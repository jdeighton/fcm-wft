"""Entry point: `python -m wft [--env prod|uat]`."""
from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

import uvicorn

from wft.config import ENVIRONMENTS, ConfigError, load_config
from wft.definitions import DefinitionRegistry
from wft.readmodel import ReadModel
from wft.repository import FileRepository, InstanceLock, InstanceLockError, default_identity
from wft.services import Services
from wft.web.app import create_app

HOST = "127.0.0.1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wft", description="Workflow tracker")
    parser.add_argument("--env", choices=ENVIRONMENTS, default="prod")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.env)
    except ConfigError as e:
        print(f"WFT: {e}", file=sys.stderr)
        return 2
    if not config.share_root.is_dir():
        print(f"WFT: share root not reachable: {config.share_root}", file=sys.stderr)
        return 2

    user, host = default_identity()
    lock = InstanceLock(config.share_root / "instance-locks" / f"{user}.lock", user, host)
    try:
        lock.acquire()
    except InstanceLockError as e:
        print(f"WFT: {e}", file=sys.stderr)
        return 3

    try:
        registry = DefinitionRegistry.load(config.definitions_dir)
        read_model = ReadModel(config.cache_path, registry)
        services = Services(FileRepository(config.share_root, user), read_model, registry, user, host)
        services.sync()
        app = create_app(services, config.env)

        url = f"http://{HOST}:{config.port}/"
        print(f"WFT ({config.env}) running at {url} as {user}. Press Ctrl+C to stop.")
        if not args.no_browser:
            threading.Timer(1.0, webbrowser.open, args=(url,)).start()
        uvicorn.run(app, host=HOST, port=config.port, log_level="warning")
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
