"""Tests for the HTTP transport: status mapping, guards, auth, backoff."""

import asyncio
from typing import Any, ClassVar, Self, cast
from unittest.mock import Mock

import aiohttp
import pytest

from huepy import models
from huepy.client.http import (
    DELAY_MAX,
    GET_RETRIES_MAX,
    RETRIES_MAX,
    HueHttpClient,
    PendingWrite,
    SSEFrame,
    backoff_delay,
)
from huepy.config import HueConfig
from huepy.exceptions import (
    AuthenticationError,
    BridgeConnectionError,
    HueAPIError,
    HueResponseError,
)
from huepy.models.common import unwrap

PATH = "/clip/v2/resource/light"


class FakeResponse:
    def __init__(self, status: int = 200, payload: Any = None, text: str = "") -> None:
        self.status = status
        self._payload = payload
        self._text = text
        self.content_length = None if payload is None else 1

    async def json(self) -> Any:
        return self._payload

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class FakeSession:
    """Stands in for aiohttp.ClientSession for the request methods."""

    def __init__(self, response: FakeResponse | list[FakeResponse]) -> None:
        self.responses = response if isinstance(response, list) else [response]
        self.calls: list[tuple[str, str, Any]] = []
        self.headers: dict[str, str] = {}

    @property
    def response(self) -> FakeResponse:
        """Return each queued response once, then retain the final response."""
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]

    def request(
        self, method: str, path: str, json: Any = None, **_: Any
    ) -> FakeResponse:
        self.calls.append((method, path, json))
        return self.response

    def post(self, path: str, json: Any = None, **_: Any) -> FakeResponse:
        self.calls.append(("POST", path, json))
        return self.response


@pytest.fixture
def config(tmp_path):
    return HueConfig(
        bridge_ip="10.0.0.1", app_key="k", config_path=tmp_path / "config.json"
    )


def make_client(
    config,
    response: FakeResponse | list[FakeResponse],
) -> tuple[HueHttpClient, FakeSession]:
    """Build a client wired to a fake session, returning both."""
    client = HueHttpClient(config)
    session = FakeSession(response)
    client.session = cast("aiohttp.ClientSession", cast("object", session))
    return client, session


class TestRequests:
    async def test_get_returns_payload(self, config):
        client, _session = make_client(
            config, FakeResponse(200, {"data": [{"id": "a"}]})
        )
        assert await client.get(PATH) == {"data": [{"id": "a"}]}

    async def test_put_sends_json_body(self, config):
        client, session = make_client(config, FakeResponse(200, {"data": []}))
        await client.put(f"{PATH}/a", {"on": {"on": True}})
        assert session.calls[-1] == ("PUT", f"{PATH}/a", {"on": {"on": True}})

    async def test_post_sends_json_body(self, config):
        client, session = make_client(config, FakeResponse(200, {"data": []}))
        await client.post(PATH, {"metadata": {}})
        assert session.calls[-1] == ("POST", PATH, {"metadata": {}})

    async def test_delete_uses_the_delete_verb(self, config):
        client, session = make_client(config, FakeResponse(200, {"data": []}))
        await client.delete(f"{PATH}/a")
        assert session.calls[-1] == ("DELETE", f"{PATH}/a", None)

    async def test_empty_body_returns_none(self, config):
        client, _session = make_client(config, FakeResponse(200))
        assert await client.get(PATH) is None

    async def test_get_retries_429_and_503_then_returns_payload(
        self, config, monkeypatch
    ):
        async def no_wait(_delay: float) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", no_wait)
        client, session = make_client(
            config,
            [
                FakeResponse(429, text="busy"),
                FakeResponse(503, text="starting"),
                FakeResponse(200, {"data": [{"id": "a"}]}),
            ],
        )

        assert await client.get(PATH) == {"data": [{"id": "a"}]}
        assert len(session.calls) == 3

    async def test_get_retry_is_bounded(self, config, monkeypatch):
        async def no_wait(_delay: float) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", no_wait)
        client, session = make_client(config, FakeResponse(429, text="busy"))

        with pytest.raises(HueAPIError) as excinfo:
            await client.get(PATH)

        assert excinfo.value.status_code == 429
        assert len(session.calls) == GET_RETRIES_MAX + 1

    @pytest.mark.parametrize("method", ["put", "post", "delete"])
    async def test_mutating_requests_are_never_retried(self, config, method):
        client, session = make_client(config, FakeResponse(503, text="unavailable"))
        call = {
            "put": lambda: client.put(PATH, {}),
            "post": lambda: client.post(PATH, {}),
            "delete": lambda: client.delete(PATH),
        }[method]

        with pytest.raises(HueAPIError):
            await call()

        assert len(session.calls) == 1


class TestWriteObserver:
    async def test_put_publishes_pending_then_accepted(self, config):
        client, _session = make_client(
            config,
            FakeResponse(200, {"errors": [], "data": [{"rid": "a"}]}),
        )
        observed: list[PendingWrite] = []
        unsubscribe = client.add_write_observer(observed.append)
        await client.put(f"{PATH}/a", {"on": {"on": True}})
        await asyncio.sleep(0)
        unsubscribe()

        assert [write.status for write in observed] == ["pending", "accepted"]
        assert observed[0].command_id == observed[1].command_id
        assert observed[0].payload == {"on": {"on": True}}
        assert observed[1].completed_at is not None

    async def test_blocking_envelope_error_is_reported_as_rejected(self, config):
        client, _session = make_client(
            config,
            FakeResponse(
                207,
                {
                    "errors": [{"error_code": "client_error"}],
                    "data": [{"rid": "a"}],
                },
            ),
        )
        observed: list[PendingWrite] = []
        _ = client.add_write_observer(observed.append)
        _ = await client.put(f"{PATH}/a", {"on": {"on": True}})
        await asyncio.sleep(0)
        assert [write.status for write in observed] == ["pending", "rejected"]

    @pytest.mark.parametrize(
        "payload",
        [None, [], {"data": []}, {"errors": []}],
        ids=["null", "list", "missing-errors", "missing-data"],
    )
    async def test_malformed_success_body_has_unknown_write_outcome(
        self, config, payload
    ):
        client, _session = make_client(config, FakeResponse(200, payload))
        observed: list[PendingWrite] = []
        _ = client.add_write_observer(observed.append)

        _ = await client.put(f"{PATH}/a", {"on": {"on": True}})

        assert [write.status for write in observed] == ["pending", "unknown"]


class TestErrorMapping:
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
    @pytest.mark.parametrize("method", ["get", "put", "post", "delete"])
    async def test_non_200_raises_hue_api_error(
        self, config, status, method, monkeypatch
    ):
        async def no_wait(_delay: float) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", no_wait)
        client, _session = make_client(config, FakeResponse(status, text="nope"))
        call = {
            "get": lambda: client.get(PATH),
            "put": lambda: client.put(PATH, {}),
            "post": lambda: client.post(PATH, {}),
            "delete": lambda: client.delete(PATH),
        }[method]
        with pytest.raises(HueAPIError) as excinfo:
            await call()
        assert excinfo.value.status_code == status
        assert "nope" in str(excinfo.value)


class TestSessionLifecycle:
    async def test_app_key_is_sent_as_header(self, config):
        async with HueHttpClient(config) as client:
            session = client.session
            assert session is not None
            assert session.headers["hue-application-key"] == "k"

    async def test_no_auth_header_without_a_key(self, tmp_path):
        config = HueConfig(bridge_ip="10.0.0.1", config_path=tmp_path / "config.json")
        async with HueHttpClient(config) as client:
            session = client.session
            assert session is not None
            assert "hue-application-key" not in session.headers

    async def test_session_caps_connections_per_bridge(self, config):
        async with HueHttpClient(config) as client:
            session = client.session
            assert session is not None
            connector = session.connector
            assert connector is not None
            assert connector.limit_per_host == 3

    async def test_session_is_cleared_on_exit(self, config):
        """Leaving a closed session in place made post-close calls fail obscurely."""
        client = HueHttpClient(config)
        await client.__aenter__()
        assert client.session is not None
        await client.__aexit__(None, None, None)
        assert client.session is None

    async def test_use_after_close_raises_runtime_error(self, config):
        client = HueHttpClient(config)
        await client.__aenter__()
        await client.__aexit__(None, None, None)
        with pytest.raises(RuntimeError, match="Client not initialized"):
            await client.get(PATH)


class TestEventConnections:
    async def test_closing_unconsumed_connection_closes_response(self, config):
        class StreamingResponse:
            status = 200

            def __init__(self) -> None:
                self.closed = False

            async def text(self) -> str:
                return ""

            def close(self) -> None:
                self.closed = True

        class StreamingSession:
            def __init__(self, response: StreamingResponse) -> None:
                self.response = response

            async def get(self, *_args: object, **_kwargs: object):
                return self.response

        response = StreamingResponse()
        client = HueHttpClient(config)
        client.session = cast(
            "aiohttp.ClientSession",
            cast("object", StreamingSession(response)),
        )
        connections = client.event_connections(max_retries=0)

        _ = await anext(connections)
        await connections.aclose()

        assert response.closed is True

    @pytest.mark.parametrize(
        "call",
        [
            lambda c: c.get(PATH),
            lambda c: c.put(PATH, {}),
            lambda c: c.post(PATH, {}),
            lambda c: c.delete(PATH),
            lambda c: c.authenticate(),
            lambda c: anext(c.subscribe_events()),
        ],
    )
    async def test_use_before_open_raises_runtime_error(self, config, call):
        with pytest.raises(RuntimeError, match="Client not initialized"):
            await call(HueHttpClient(config))


class TestAuthenticate:
    async def test_success_saves_and_applies_the_key(self, config):
        client, session = make_client(
            config,
            FakeResponse(200, [{"success": {"username": "new-key"}}]),
        )
        key = await client.authenticate()
        assert key == "new-key"
        assert config.app_key == "new-key"
        assert session.headers["hue-application-key"] == "new-key"
        assert config.config_path.is_file()

    async def test_bridge_error_raises(self, config):
        client, _session = make_client(
            config,
            FakeResponse(200, [{"error": {"type": 7, "description": "invalid value"}}]),
        )
        with pytest.raises(AuthenticationError, match="invalid value"):
            await client.authenticate()

    async def test_timeout_when_button_never_pressed(self, config):
        client, _session = make_client(
            config,
            FakeResponse(200, [{"error": {"type": 101, "description": "link button"}}]),
        )
        with pytest.raises(AuthenticationError, match="Timed out"):
            await client.authenticate(timeout=0)


class TestBackoff:
    """Reconnect backoff must actually grow.

    Regression: DELAY_INITIAL was 0, making min(0 * 2**n, DELAY_MAX) always 0,
    so the event stream burned all its retries instantly.
    """

    @pytest.mark.parametrize(
        ("retry", "expected"), [(1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (5, 16.0)]
    )
    def test_delay_doubles_per_retry(self, retry, expected):
        assert backoff_delay(retry) == expected

    def test_first_retry_actually_waits(self):
        assert backoff_delay(1) > 0

    def test_delay_is_capped(self):
        assert backoff_delay(50) == DELAY_MAX

    def test_retry_ceiling_is_positive(self):
        assert RETRIES_MAX > 0


class TestEventStream:
    async def test_establishment_failure_raises_after_finite_retries(
        self, config, monkeypatch
    ):
        slept: list[float] = []

        async def fake_sleep(delay: float) -> None:
            slept.append(delay)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        class FailingSession:
            async def get(self, *_args, **_kwargs):
                raise aiohttp.ClientConnectionError("offline")

        client = HueHttpClient(config)
        client.session = cast("aiohttp.ClientSession", cast("object", FailingSession()))

        with pytest.raises(BridgeConnectionError, match="after 2 retries"):
            _ = [frame async for frame in client.subscribe_event_frames(max_retries=2)]

        assert slept == [1.0, 2.0]

    def test_complete_frame_preserves_cursor_and_batch(self, config):
        client = HueHttpClient(config)
        frame = client._decode_sse_frame(
            "1700000000:4",
            ['[{"id":"event-1","type":"update"}]'],
        )
        assert isinstance(frame, SSEFrame)
        assert frame.event_id == "1700000000:4"
        assert frame.events == [{"id": "event-1", "type": "update"}]


class TestMultiStatus:
    """The bridge answers partial failures with 207, detail in the body.

    Regression: only 200 was accepted, so a 207 surfaced as HueAPIError
    carrying raw JSON instead of the HueResponseError built for this case.
    A real bridge produced exactly this when asked to set colour temperature
    on a light that does not support it.
    """

    REJECTION: ClassVar[dict[str, object]] = {
        "data": [{"rid": "17abe584", "rtype": "light"}],
        "errors": [
            {
                "description": (
                    "attribute (.color_temperature.mirek) is not supported "
                    "by resource 17abe584"
                ),
                "error_code": "client_error",
            }
        ],
    }

    async def test_207_body_is_returned_not_raised_as_transport_error(self, config):
        client, _session = make_client(config, FakeResponse(207, self.REJECTION))
        assert await client.put(PATH, {}) == self.REJECTION

    async def test_207_reaches_the_envelope_as_a_response_error(self, config):
        client, _session = make_client(config, FakeResponse(207, self.REJECTION))
        payload = await client.put(PATH, {})
        with pytest.raises(HueResponseError, match=r"not supported"):
            unwrap(payload, models.ResourceIdentifier)

    @pytest.mark.parametrize("status", [200, 201, 204, 207, 299])
    async def test_every_2xx_is_accepted(self, config, status):
        client, _session = make_client(config, FakeResponse(status, {"data": []}))
        assert await client.get(PATH) == {"data": []}

    @pytest.mark.parametrize("status", [300, 301, 304, 400, 500])
    async def test_non_2xx_still_raises(self, config, status):
        client, _session = make_client(config, FakeResponse(status, text="nope"))
        with pytest.raises(HueAPIError):
            await client.get(PATH)


class TestConnectionFailures:
    """A bridge that cannot be reached must not leak an aiohttp exception.

    Regression: only authenticate() wrapped ClientError, so an ordinary GET
    against an unreachable bridge raised ClientConnectorError at the caller.
    """

    class ExplodingSession:
        def __init__(self, exc: BaseException) -> None:
            self.exc = exc
            self.headers: dict[str, str] = {}

        def request(self, *_args: object, **_kwargs: object) -> object:
            raise self.exc

    @pytest.mark.parametrize(
        "exc",
        [
            aiohttp.ClientConnectorError(Mock(), OSError("unreachable")),
            aiohttp.ClientOSError("boom"),
            TimeoutError(),
        ],
    )
    async def test_becomes_bridge_connection_error(self, config, exc):
        client = HueHttpClient(config)
        client.session = cast(
            "aiohttp.ClientSession", cast("object", self.ExplodingSession(exc))
        )
        with pytest.raises(BridgeConnectionError, match=r"10\.0\.0\.1"):
            await client.get(PATH)
