"""Shared fixtures for the huepy test suite.

Resource tests exercise request *shaping* -- which path is hit, what payload is
sent, and how the response parses -- so they run against an in-memory stand-in
for the HTTP client rather than a socket. Responses are shaped like real v2
bodies (``{"errors": [...], "data": [...]}``) so the envelope handling is
exercised too.
"""

from collections.abc import AsyncGenerator
from types import TracebackType
from typing import Any, Self

import pytest

from huepy.client.base import Hue

Call = tuple[str, str, dict[str, Any] | None]

RESOURCE_ROOT = "/clip/v2/resource"


def envelope(*data: dict[str, Any], errors: list[str] | None = None) -> dict[str, Any]:
    """Build a v2 response body around the given resources."""
    return {
        "errors": [{"description": e} for e in (errors or [])],
        "data": list(data),
    }


class FakeHttp:
    """Records every request and replays canned responses."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._responses: dict[str, Any] = {}
        self.write_result: Any = envelope({"rid": "updated-id", "rtype": "light"})
        self.closed = False
        self.events: list[dict[str, Any]] = []

    def queue(self, path: str, payload: Any) -> None:
        """Register the body returned for a GET on ``path``."""
        self._responses[path] = payload

    def queue_resource(
        self,
        resource_type: str,
        resource_id: str,
        body: dict[str, Any],
    ) -> None:
        """Register a single-resource GET."""
        self.queue(f"{RESOURCE_ROOT}/{resource_type}/{resource_id}", envelope(body))

    def queue_collection(
        self,
        resource_type: str,
        bodies: list[dict[str, Any]],
    ) -> None:
        """Register a collection GET."""
        self.queue(f"{RESOURCE_ROOT}/{resource_type}", envelope(*bodies))

    async def get(self, path: str) -> Any:
        self.calls.append(("GET", path, None))
        return self._responses.get(path, envelope())

    async def put(self, path: str, data: dict[str, Any]) -> Any:
        self.calls.append(("PUT", path, data))
        return self.write_result

    async def post(self, path: str, data: dict[str, Any]) -> Any:
        self.calls.append(("POST", path, data))
        return self.write_result

    async def delete(self, path: str) -> Any:
        self.calls.append(("DELETE", path, None))
        return self.write_result

    async def authenticate(
        self,
        app_name: str = "huepy",
        timeout: int = 60,  # noqa: ASYNC109 - mirrors the Transport protocol
    ) -> str:
        self.calls.append(("AUTH", app_name, {"timeout": timeout}))
        return "fake-app-key"

    async def subscribe_events(self) -> AsyncGenerator[dict[str, Any]]:
        for event in self.events:
            yield event

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_val: BaseException | None = None,
        exc_tb: TracebackType | None = None,
    ) -> None:
        self.closed = True

    @property
    def last(self) -> Call:
        """The most recent call, as ``(method, path, payload)``."""
        return self.calls[-1]

    @property
    def paths(self) -> list[str]:
        return [path for _, path, _ in self.calls]

    @property
    def writes(self) -> list[Call]:
        """Only the mutating calls -- a read often precedes the write."""
        return [call for call in self.calls if call[0] in {"PUT", "POST", "DELETE"}]


@pytest.fixture
def http() -> FakeHttp:
    return FakeHttp()


@pytest.fixture
def hue(http: FakeHttp, tmp_path) -> Hue:
    """Build a started Hue client wired to the fake transport."""
    client = Hue(
        bridge_ip="10.0.0.1",
        app_key="test-app-key",
        config_path=tmp_path / "config.json",
    )
    client._http = http
    return client


@pytest.fixture
def bare_hue(tmp_path) -> Hue:
    """Build a Hue client that was never started -- no transport attached."""
    return Hue(
        bridge_ip="10.0.0.1",
        app_key="test-app-key",
        config_path=tmp_path / "config.json",
    )
