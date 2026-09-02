"""The HTTP signal server, driven by a real loopback client on a free port."""

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from huepy.exceptions import PlanError
from huepy.plans.signals import SignalServer

KNOWN = {"movie_started", "movie_ended"}
GUARD = "s3cret"
WRONG_GUARD = "nope"


def request(
    method: str, url: str, *, token: str | None = None
) -> tuple[int, dict[str, Any]]:
    """Send one request from a worker thread and decode the JSON answer."""
    built = urllib.request.Request(url, method=method)  # noqa: S310 - loopback, built here
    if token is not None:
        built.add_header("Authorization", f"Bearer {token}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(built, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        body = (
            json.load(error)
            if error.headers.get_content_type() == "application/json"
            else {}
        )
        return error.code, body


async def post(url: str, *, token: str | None = None) -> tuple[int, dict[str, Any]]:
    return await asyncio.to_thread(request, "POST", url, token=token)


async def get(url: str, *, token: str | None = None) -> tuple[int, dict[str, Any]]:
    return await asyncio.to_thread(request, "GET", url, token=token)


class Recording:
    def __init__(self) -> None:
        self.fired: list[str] = []

    def fire(self, name: str) -> tuple[str, ...]:
        self.fired.append(name)
        return (f"activated {name!r}",)


@pytest.fixture
async def served():
    recording = Recording()
    async with SignalServer(recording.fire, KNOWN, port=0) as server:
        yield f"http://127.0.0.1:{server.port}", recording


class TestServer:
    async def test_a_known_signal_fires_and_returns_the_outcomes(self, served):
        url, recording = served
        status, body = await post(f"{url}/signals/movie_started")
        assert status == 200
        assert body == {
            "signal": "movie_started",
            "outcomes": ["activated 'movie_started'"],
        }
        assert recording.fired == ["movie_started"]

    async def test_an_unknown_signal_is_refused_with_the_known_ones(self, served):
        url, recording = served
        status, body = await post(f"{url}/signals/doorbell")
        assert status == 404
        assert body == {
            "error": "unknown signal",
            "signal": "doorbell",
            "known": ["movie_ended", "movie_started"],
        }
        assert recording.fired == []

    async def test_get_lists_what_the_plan_listens_for(self, served):
        url, _ = served
        assert await get(f"{url}/signals") == (
            200,
            {"signals": ["movie_ended", "movie_started"]},
        )

    async def test_firing_needs_post(self, served):
        url, recording = served
        status, _ = await get(f"{url}/signals/movie_started")
        assert status == 405
        assert recording.fired == []

    async def test_a_failing_fire_is_a_500_and_the_server_survives(self):
        calls: list[str] = []

        def fire(name: str) -> tuple[str, ...]:
            calls.append(name)
            if len(calls) == 1:
                msg = "boom"
                raise RuntimeError(msg)
            return ("ok",)

        async with SignalServer(fire, {"x"}, port=0) as server:
            url = f"http://127.0.0.1:{server.port}/signals/x"
            status, body = await post(url)
            assert status == 500
            assert body["error"] == "the plan could not act on the signal"
            assert (await post(url))[0] == 200


class TestToken:
    @pytest.fixture
    async def guarded(self):
        recording = Recording()
        server = SignalServer(recording.fire, KNOWN, port=0, token=GUARD)
        async with server:
            yield f"http://127.0.0.1:{server.port}", recording

    async def test_without_the_token_is_401(self, guarded):
        url, recording = guarded
        assert (await post(f"{url}/signals/movie_started"))[0] == 401
        assert (await post(f"{url}/signals/movie_started", token=WRONG_GUARD))[0] == 401
        assert (await get(f"{url}/signals"))[0] == 401
        assert recording.fired == []

    async def test_with_the_token_it_fires(self, guarded):
        url, recording = guarded
        assert (await post(f"{url}/signals/movie_started", token=GUARD))[0] == 200
        assert recording.fired == ["movie_started"]

    def test_listening_beyond_loopback_needs_a_token(self):
        with pytest.raises(PlanError, match="other machines"):
            _ = SignalServer(Recording().fire, KNOWN, host="0.0.0.0")  # noqa: S104 - the point of the test

    def test_a_token_allows_it(self):
        server = SignalServer(Recording().fire, KNOWN, host="0.0.0.0", token=GUARD)  # noqa: S104 - the point of the test
        assert server.host == "0.0.0.0"  # noqa: S104 - the point of the test

    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
    def test_loopback_needs_none(self, host):
        assert SignalServer(Recording().fire, KNOWN, host=host).host == host


class TestLifecycle:
    def test_the_port_before_starting_is_an_error(self):
        with pytest.raises(RuntimeError, match="not been started"):
            _ = SignalServer(Recording().fire, KNOWN, port=0).port

    async def test_closing_releases_the_port(self):
        server = SignalServer(Recording().fire, KNOWN, port=0)
        await server.start()
        url = f"http://127.0.0.1:{server.port}/signals"
        await server.close()
        # Refused, or reset mid-handshake while the port is being released:
        # either way nothing answers, and both are OSError.
        with pytest.raises((ConnectionError, urllib.error.URLError)):
            _ = await get(url)
