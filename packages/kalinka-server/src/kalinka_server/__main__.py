#!/usr/bin/env python3
"""Main entry point for the Kalinka server when run as a module."""
import argparse
import asyncio
import logging
import os
from asyncio import CancelledError

import uvicorn

from kalinka_plugin_sdk import paths

from .config_model import KalinkaConfig
from .config_overrides import apply_overrides_with_prefix, load_overrides
from .logging_setup import (
    make_handler,
    quiet_credential_carrying_loggers,
    uvicorn_log_config,
)
from .netutils import get_ip_address
from .sdk_compat import IncompatibleSDKError, check_sdk_compatibility
from .server import create_app
from .state_keeper import set_state_file


logger = logging.getLogger(__name__.split(".")[-1])


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        action="store",
        default=os.path.join(paths.etc_dir(), "kalinka_conf.cfg"),
        help="Config file location (default: <KALINKA_PREFIX>/etc/kalinka/kalinka_conf.cfg)",
    )
    parser.add_argument(
        "--state",
        action="store",
        default=os.path.join(paths.state_dir(), "kalinka_state.json"),
        help="State file location (default: <KALINKA_PREFIX>/var/lib/kalinka/kalinka_state.json)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Set log level to debug",
        default=False,
    )
    return parser.parse_args()


async def main():
    """Main entry point for the Kalinka server."""
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug is True else logging.INFO,
        handlers=[make_handler()],
    )

    quiet_credential_carrying_loggers()

    # Refuse to run against an incompatible plugin SDK (see sdk_compat.py).
    try:
        check_sdk_compatibility()
    except IncompatibleSDKError as e:
        logger.error("%s", e)
        raise SystemExit(1)

    try:
        config = KalinkaConfig()
        overrides = load_overrides(args.config)
        apply_overrides_with_prefix(config, overrides, "base_config.")

        if args.state:
            set_state_file(args.state)

        host = get_ip_address(config.server.interface)
        port = config.server.port
        logger.info(f"Starting server on {host}:{port}")
        app = await create_app(args.config, config, overrides, bind_host=host)
        uvicorn_config = uvicorn.Config(
            app,
            host=host,
            port=port,
            reload=False,
            timeout_graceful_shutdown=5,
            log_config=uvicorn_log_config(args.debug),
        )
        server = uvicorn.Server(uvicorn_config)
        await server.serve()
        logger.info("Server shut down")

    except KeyboardInterrupt:
        logger.info("Server shut down")
    except CancelledError:
        logger.info("Server shut down")
    except Exception as e:
        logger.error(f"Error starting server: {e}")
        raise


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
