"""One-time setup: store the bridge address and obtain an application key.

Run once per bridge, passing its address:

    python examples/authenticate.py 192.168.1.100

Both settings are written to ``$XDG_CONFIG_HOME/huepy/config.json`` (or
``HUE_CONFIG_PATH``), restricted to your user, so every other example runs with
no arguments and no environment afterwards. Re-run it with an address to record
a new one, e.g. after the bridge changes IP.
"""

import asyncio
import os
import sys

from huepy import (
    AuthenticationError,
    BridgeConnectionError,
    Hue,
    HueConfig,
)
from huepy.config import ENV_BRIDGE_IP


def resolve_bridge_ip() -> str | None:
    """Take the address from argv, else the environment, else the config file."""
    if len(sys.argv) > 1:
        return sys.argv[1]
    return os.getenv(ENV_BRIDGE_IP) or None


async def request_key(config: HueConfig) -> str | None:
    """Ask the bridge for a key; the link button must already be pressed.

    Entering the client on a bridge that has issued no key yet skips the
    id-to-name lookup, so the ordinary ``async with Hue(...)`` works here too.
    A key that is issued is stored on the way out.
    """
    async with Hue(bridge_ip=config.bridge_ip, config_path=config.config_path) as hue:
        try:
            return await hue.authenticate()
        except (AuthenticationError, BridgeConnectionError) as exc:
            print(f"Authentication failed: {exc}")
            return None


def main() -> None:
    bridge_ip = resolve_bridge_ip()
    try:
        config = HueConfig(bridge_ip=bridge_ip or "")
    except ValueError as exc:
        print(exc)
        print("\nPass the bridge address: python examples/authenticate.py <ip>")
        raise SystemExit(1) from None

    if config.app_key:
        # Nothing to authenticate; just make sure the address is on disk so the
        # other examples need no arguments.
        config.save()
        print(f"Already authenticated. Settings stored in {config.config_path}")
        print(f"  bridge_ip = {config.bridge_ip}")
        return

    print(f"Bridge: {config.bridge_ip}")
    print("Press the link button on your Hue bridge, then press Enter...")
    input()

    app_key = asyncio.run(request_key(config))
    if app_key is None:
        raise SystemExit(1)

    print(f"Success. Settings stored in {config.config_path} (owner-only).")
    print(f"  bridge_ip = {config.bridge_ip}")
    print(f"  app_key   = {app_key[:8]}...")


if __name__ == "__main__":
    main()
