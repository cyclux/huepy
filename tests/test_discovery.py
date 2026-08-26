"""Tests for bridge discovery.

Discovery reaches the network through an injectable ``aiohttp``-shaped session,
so these tests pass in doubles rather than opening sockets, and monkeypatch the
mDNS helper so no multicast is attempted.
"""

from typing import Any, Self, cast

import aiohttp
import pytest

from huepy.client import discovery
from huepy.client.discovery import DiscoveredBridge, discover, discover_bridge_id
from huepy.exceptions import BridgeConnectionError

CLOUD = "https://discovery.meethue.com"

CLOUD_BODY = [
    {
        "id": "001788FFFE100491",
        "internalipaddress": "192.168.2.23",
        "macaddress": "00:17:88:10:04:91",
        "name": "Philips Hue",
    },
    {"id": "001788FFFE09A168", "internalipaddress": "192.168.88.252"},
]


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        payload: Any = None,
        raises: Exception | None = None,
    ) -> None:
        self.status = status
        self._payload = payload
        self._raises = raises

    async def json(self) -> Any:
        return self._payload

    async def __aenter__(self) -> Self:
        if self._raises is not None:
            raise self._raises
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class FakeSession:
    """Maps a URL substring to the response returned for a GET on it."""

    def __init__(self, by_url: dict[str, FakeResponse]) -> None:
        self.by_url = by_url
        self.requested: list[str] = []
        self.closed = False

    def get(self, url: str, **_: Any) -> FakeResponse:
        self.requested.append(url)
        for key, response in self.by_url.items():
            if key in url:
                return response
        return FakeResponse(404)

    async def close(self) -> None:
        self.closed = True


def as_session(fake: FakeSession) -> aiohttp.ClientSession:
    return cast("aiohttp.ClientSession", cast("object", fake))


def config_url(ip: str) -> str:
    return f"https://{ip}/api/0/config"


class TestCloud:
    async def test_lists_bridges_without_validation(self):
        session = FakeSession({CLOUD: FakeResponse(200, CLOUD_BODY)})
        bridges = await discover(
            method="cloud", validate=False, session=as_session(session)
        )
        assert [(b.bridge_id, b.ip) for b in bridges] == [
            ("001788fffe100491", "192.168.2.23"),
            ("001788fffe09a168", "192.168.88.252"),
        ]

    async def test_hits_the_exact_cloud_url(self):
        session = FakeSession({CLOUD: FakeResponse(200, [])})
        await discover(method="cloud", validate=False, session=as_session(session))
        assert session.requested == [CLOUD]

    async def test_empty_array_means_no_bridges(self):
        session = FakeSession({CLOUD: FakeResponse(200, [])})
        bridges = await discover(
            method="cloud", validate=False, session=as_session(session)
        )
        assert bridges == []

    async def test_non_200_means_no_bridges(self):
        session = FakeSession({CLOUD: FakeResponse(429, None)})
        bridges = await discover(
            method="cloud", validate=False, session=as_session(session)
        )
        assert bridges == []


class TestValidation:
    async def test_enriches_and_drops_unreachable(self):
        session = FakeSession(
            {
                CLOUD: FakeResponse(200, CLOUD_BODY),
                config_url("192.168.2.23"): FakeResponse(
                    200,
                    {
                        "bridgeid": "001788FFFE100491",
                        "modelid": "BSB002",
                        "swversion": "1948086000",
                        "apiversion": "1.48.0",
                    },
                ),
                config_url("192.168.88.252"): FakeResponse(
                    raises=aiohttp.ClientError("unreachable")
                ),
            }
        )
        bridges = await discover(
            method="cloud", validate=True, session=as_session(session)
        )
        assert len(bridges) == 1
        bridge = bridges[0]
        assert bridge.ip == "192.168.2.23"
        assert bridge.bridge_id == "001788fffe100491"
        assert bridge.model_id == "BSB002"
        assert bridge.api_version == "1.48.0"


class TestDiscoverBridgeId:
    async def test_reads_the_bridge_id(self):
        session = FakeSession(
            {config_url("10.0.0.1"): FakeResponse(200, {"bridgeid": "001788FFFEAAA"})}
        )
        assert (
            await discover_bridge_id("10.0.0.1", session=as_session(session))
            == "001788fffeaaa"
        )

    async def test_unreachable_raises(self):
        session = FakeSession(
            {config_url("10.0.0.1"): FakeResponse(raises=aiohttp.ClientError())}
        )
        with pytest.raises(BridgeConnectionError):
            await discover_bridge_id("10.0.0.1", session=as_session(session))


class TestMdnsAndAuto:
    async def test_mdns_returns_browsed_bridges(self, monkeypatch):
        found = [DiscoveredBridge(bridge_id="abc", ip="192.168.1.5")]

        async def fake_mdns(_timeout: float) -> list[DiscoveredBridge]:
            return found

        monkeypatch.setattr(discovery, "_discover_mdns", fake_mdns)
        session = FakeSession({})
        bridges = await discover(
            method="mdns", validate=False, session=as_session(session)
        )
        assert bridges == found

    async def test_auto_falls_back_to_cloud_when_mdns_is_empty(self, monkeypatch):
        async def empty_mdns(_timeout: float) -> list[DiscoveredBridge]:
            return []

        monkeypatch.setattr(discovery, "_discover_mdns", empty_mdns)
        session = FakeSession({CLOUD: FakeResponse(200, CLOUD_BODY)})
        bridges = await discover(
            method="auto", validate=False, session=as_session(session)
        )
        assert [b.ip for b in bridges] == ["192.168.2.23", "192.168.88.252"]


class FakeServiceInfo:
    def __init__(self, addresses: list[str], properties: dict[bytes, bytes]) -> None:
        self._addresses = addresses
        self.properties = properties

    def parsed_addresses(self) -> list[str]:
        return self._addresses


class TestMdnsExtraction:
    def test_prefers_ipv4_and_parses_txt(self):
        info = FakeServiceInfo(
            ["fe80::1", "192.168.1.9"],
            {b"bridgeid": b"001788FFFEABC", b"modelid": b"BSB002"},
        )
        bridge = discovery._bridge_from_mdns(info)
        assert bridge is not None
        assert bridge.ip == "192.168.1.9"  # IPv4 chosen over the IPv6 record
        assert bridge.bridge_id == "001788fffeabc"
        assert bridge.model_id == "BSB002"

    def test_incomplete_records_return_none(self):
        assert discovery._bridge_from_mdns(None) is None
        no_addr = FakeServiceInfo([], {b"bridgeid": b"x"})
        assert discovery._bridge_from_mdns(no_addr) is None
        no_id = FakeServiceInfo(["1.2.3.4"], {})
        assert discovery._bridge_from_mdns(no_id) is None
