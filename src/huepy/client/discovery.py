"""Find Hue bridges on the network, before any bridge is configured.

Discovery runs before a bridge address, application key, or bridge id exists, so
it cannot use :class:`~huepy.client.http.HueHttpClient` -- that client is pinned
to one bridge. It is the second place ``aiohttp`` is allowed (the first being
``client/http.py``), because it must reach the cloud discovery endpoint and read
a candidate bridge's unauthenticated config over HTTPS.

Three methods are offered, matching the developer guidance:

* **mDNS** -- browse ``_hue._tcp.local`` on the local network. Works offline and
  is not rate limited, but needs multicast to reach the bridge.
* **cloud** -- ``GET https://discovery.meethue.com``, which returns the bridges
  the caller's public IP is associated with. Rate limited to one request per 15
  minutes per client, so discover once and store the address.
* **manual** -- not a method here; supplying ``bridge_ip=`` to :class:`Hue`
  stays the fallback.

The deprecated UPnP/SSDP method is deliberately not implemented.

Typical usage example:

    bridges = await discover()
    if bridges:
        hue = Hue(bridge_ip=bridges[0].ip, bridge_id=bridges[0].bridge_id)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

import aiohttp
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from huepy.client._ssl import unverified_ssl_context

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["DiscoveredBridge", "discover", "discover_bridge_id"]

CLOUD_DISCOVERY_URL = "https://discovery.meethue.com"
MDNS_SERVICE = "_hue._tcp.local."
DEFAULT_TIMEOUT = 5.0

# A bridge's /api/0/config is read over its self-signed-style cert before its
# identity is known, so verification is off. One context is safe to share.
_UNVERIFIED_SSL = unverified_ssl_context()

DiscoveryMethod = Literal["auto", "cloud", "mdns"]


@dataclass(frozen=True)
class DiscoveredBridge:
    """A Hue bridge found on the network.

    Attributes:
        bridge_id: The bridge id, used to pin its TLS certificate.
        ip: The bridge's address on the local network.
        model_id: The bridge model, when validation filled it in.
        sw_version: The bridge firmware version, when known.
        api_version: The CLIP API version the bridge reports, when known.

    """

    bridge_id: str
    ip: str
    model_id: str | None = None
    sw_version: str | None = None
    api_version: str | None = None


async def discover(
    *,
    method: DiscoveryMethod = "auto",
    validate: bool = True,
    timeout: float = DEFAULT_TIMEOUT,  # noqa: ASYNC109 - timeout arg is by design
    session: aiohttp.ClientSession | None = None,
) -> list[DiscoveredBridge]:
    """Find Hue bridges reachable from this machine.

    Args:
        method: ``"mdns"``, ``"cloud"``, or ``"auto"`` to try mDNS first and
            fall back to the cloud endpoint when it finds nothing.
        validate: Whether to confirm each candidate by reading its
            unauthenticated ``/api/0/config``, which also fills in the bridge
            id and versions. Candidates that do not answer are dropped.
        timeout: Seconds to spend on each network step.
        session: An HTTP session to use; one is created and closed when omitted.
            Injectable for tests.

    Returns:
        The bridges found, de-duplicated by address, in a stable order.

    """
    owns_session = session is None
    session = session or aiohttp.ClientSession()
    try:
        candidates = await _find_candidates(method, timeout, session)
        if not validate:
            return _dedupe(candidates)
        validated = await asyncio.gather(
            *(_validate(candidate, timeout, session) for candidate in candidates)
        )
        return _dedupe([bridge for bridge in validated if bridge is not None])
    finally:
        if owns_session:
            await session.close()


async def _find_candidates(
    method: DiscoveryMethod,
    timeout: float,  # noqa: ASYNC109 - timeout arg is by design
    session: aiohttp.ClientSession,
) -> list[DiscoveredBridge]:
    """Run the requested discovery method(s) and return unvalidated candidates."""
    if method == "cloud":
        return await _discover_cloud(timeout, session)
    if method == "mdns":
        return await _discover_mdns(timeout)
    found = await _discover_mdns(timeout)
    if found:
        return found
    return await _discover_cloud(timeout, session)


async def _discover_cloud(
    timeout: float,  # noqa: ASYNC109 - timeout arg is by design
    session: aiohttp.ClientSession,
) -> list[DiscoveredBridge]:
    """Ask the Hue cloud which bridges share this public IP."""
    try:
        async with session.get(
            CLOUD_DISCOVERY_URL,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            if response.status != 200:  # noqa: PLR2004 - HTTP OK
                return []
            payload: object = await response.json()
    except (TimeoutError, aiohttp.ClientError, ValueError):
        # ValueError covers a malformed JSON body (JSONDecodeError), which a
        # public URL or a stray LAN host on 443 can return with a 200.
        return []
    if not isinstance(payload, list):
        return []
    bridges: list[DiscoveredBridge] = []
    for entry in cast("list[object]", payload):
        if not isinstance(entry, dict):
            continue
        record = cast("dict[str, object]", entry)
        bridge_id = record.get("id")
        ip = record.get("internalipaddress")
        if isinstance(bridge_id, str) and isinstance(ip, str):
            bridges.append(DiscoveredBridge(bridge_id=bridge_id.lower(), ip=ip))
    return bridges


async def _discover_mdns(
    timeout: float,  # noqa: ASYNC109 - timeout arg is by design
) -> list[DiscoveredBridge]:
    """Browse ``_hue._tcp.local`` for bridges advertising themselves.

    Each advertised service is resolved with ``AsyncServiceInfo.async_request``:
    modern zeroconf forbids the synchronous ``get_service_info`` from the event
    loop, so resolution is scheduled as a task from the (synchronous) browse
    callback and awaited before the browser is torn down.
    """
    found: dict[str, DiscoveredBridge] = {}
    tasks: list[asyncio.Task[None]] = []
    aiozc = AsyncZeroconf()

    async def resolve(name: str) -> None:
        info = AsyncServiceInfo(MDNS_SERVICE, name)
        if await info.async_request(aiozc.zeroconf, int(timeout * 1000)):
            bridge = _bridge_from_mdns(info)
            if bridge is not None:
                found[bridge.bridge_id] = bridge

    def on_change(
        zeroconf: object,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        # zeroconf fires this handler with keyword arguments, so the parameter
        # names are part of the contract and cannot be renamed; the browser and
        # type are unused here.
        del zeroconf, service_type
        if state_change is ServiceStateChange.Added:
            tasks.append(asyncio.ensure_future(resolve(name)))

    browser = AsyncServiceBrowser(aiozc.zeroconf, MDNS_SERVICE, handlers=[on_change])
    try:
        await asyncio.sleep(timeout)
        if tasks:
            _ = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await browser.async_cancel()
        await aiozc.async_close()
    return list(found.values())


def _bridge_from_mdns(info: object) -> DiscoveredBridge | None:
    """Extract a bridge from one resolved mDNS service record, if complete."""
    if info is None:
        return None
    addresses: Sequence[str] = cast("Any", info).parsed_addresses()
    properties: dict[bytes, bytes | None] = cast("Any", info).properties
    bridge_id = properties.get(b"bridgeid")
    if not addresses or bridge_id is None:
        return None
    # Prefer an IPv4 address: the /api/0/config validation dials it directly, and
    # a link-local IPv6 record advertised first would be unreachable.
    ip = next((address for address in addresses if ":" not in address), addresses[0])
    model = properties.get(b"modelid")
    return DiscoveredBridge(
        bridge_id=bridge_id.decode().lower(),
        ip=ip,
        model_id=model.decode() if model else None,
    )


async def _validate(
    candidate: DiscoveredBridge,
    timeout: float,  # noqa: ASYNC109 - timeout arg is by design
    session: aiohttp.ClientSession,
) -> DiscoveredBridge | None:
    """Confirm a candidate answers and enrich it from ``/api/0/config``."""
    config = await _read_bridge_config(candidate.ip, timeout, session)
    if config is None:
        return None
    bridge_id = config.get("bridgeid")
    return DiscoveredBridge(
        bridge_id=(bridge_id or candidate.bridge_id).lower(),
        ip=candidate.ip,
        model_id=config.get("modelid") or candidate.model_id,
        sw_version=config.get("swversion"),
        api_version=config.get("apiversion"),
    )


async def _read_bridge_config(
    ip: str,
    timeout: float,  # noqa: ASYNC109 - timeout arg is by design
    session: aiohttp.ClientSession,
) -> dict[str, str] | None:
    """Read a bridge's unauthenticated ``/api/0/config``, or None if unreachable."""
    try:
        async with session.get(
            f"https://{ip}/api/0/config",
            ssl=_UNVERIFIED_SSL,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            if response.status != 200:  # noqa: PLR2004 - HTTP OK
                return None
            payload: object = await response.json()
    except (TimeoutError, aiohttp.ClientError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return {
        key: value
        for key, value in cast("dict[str, object]", payload).items()
        if isinstance(value, str)
    }


async def discover_bridge_id(
    ip: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,  # noqa: ASYNC109 - timeout arg is by design
    session: aiohttp.ClientSession | None = None,
) -> str:
    """Read a bridge's id from its unauthenticated config.

    This is the value that pins the bridge's TLS certificate; fetching it over an
    unverified connection is trust-on-first-use, so prefer mDNS or the cloud
    endpoint when the identity matters.

    Args:
        ip: The bridge address.
        timeout: Seconds to wait for the bridge to answer.
        session: An HTTP session to use; one is created and closed when omitted.

    Returns:
        The bridge id.

    Raises:
        BridgeConnectionError: If the bridge cannot be reached or reports no id.

    """
    from huepy.exceptions import BridgeConnectionError  # noqa: PLC0415 - avoid cycle

    owns_session = session is None
    session = session or aiohttp.ClientSession()
    try:
        config = await _read_bridge_config(ip, timeout, session)
    finally:
        if owns_session:
            await session.close()
    bridge_id = config.get("bridgeid") if config else None
    if not bridge_id:
        msg = f"No Hue bridge answered at {ip}"
        raise BridgeConnectionError(msg)
    return bridge_id.lower()


def _dedupe(bridges: list[DiscoveredBridge]) -> list[DiscoveredBridge]:
    """Drop duplicate bridges, keeping the first seen for each address."""
    seen: set[str] = set()
    unique: list[DiscoveredBridge] = []
    for bridge in bridges:
        if bridge.ip in seen:
            continue
        seen.add(bridge.ip)
        unique.append(bridge)
    return unique
