"""Tests for client-side write pacing.

A virtual clock drives every timing assertion: the fake ``sleep`` advances the
clock and returns at once, so the tests are deterministic and never wait on the
wall.
"""

from typing import Any, Self, cast

import aiohttp
import pytest

from huepy.client.http import HueHttpClient
from huepy.client.ratelimit import GROUP_MIN_GAP, LIGHT_MIN_GAP, RateLimiter, bucket_for
from huepy.config import HueConfig

LIGHT = "/clip/v2/resource/light"
GROUPED = "/clip/v2/resource/grouped_light"
SCENE = "/clip/v2/resource/scene"


class Clock:
    """A virtual monotonic clock advanced only by the fake sleep."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


def make_limiter(clock: Clock, *, enabled: bool = True) -> RateLimiter:
    return RateLimiter(enabled=enabled, clock=clock.time, sleep=clock.sleep)


class TestBucketFor:
    def test_light_and_broadcast_and_untracked(self):
        assert bucket_for(f"{LIGHT}/a") == "light"
        assert bucket_for(f"{GROUPED}/g") == "broadcast"
        assert bucket_for(f"{SCENE}/s") == "broadcast"
        assert bucket_for("/clip/v2/resource/room/r") is None
        assert bucket_for("/eventstream/clip/v2") is None
        assert bucket_for(f"{LIGHT}") == "light"  # collection path


class TestPacing:
    async def test_consecutive_light_writes_are_spaced(self):
        clock = Clock()
        limiter = make_limiter(clock)
        await limiter.acquire(f"{LIGHT}/a")
        await limiter.acquire(f"{LIGHT}/a")
        assert clock.sleeps == [pytest.approx(LIGHT_MIN_GAP)]

    async def test_light_budget_is_shared_across_ids(self):
        clock = Clock()
        limiter = make_limiter(clock)
        await limiter.acquire(f"{LIGHT}/a")
        await limiter.acquire(f"{LIGHT}/b")  # different light, same budget
        assert clock.sleeps == [pytest.approx(LIGHT_MIN_GAP)]

    async def test_grouped_light_and_scene_share_the_broadcast_budget(self):
        clock = Clock()
        limiter = make_limiter(clock)
        await limiter.acquire(f"{GROUPED}/g")
        await limiter.acquire(f"{SCENE}/s")  # broadcast too -> shares the budget
        assert clock.sleeps == [pytest.approx(GROUP_MIN_GAP)]

    async def test_light_and_broadcast_budgets_are_independent(self):
        clock = Clock()
        limiter = make_limiter(clock)
        await limiter.acquire(f"{LIGHT}/a")
        await limiter.acquire(f"{GROUPED}/g")  # first broadcast -> no wait
        assert clock.sleeps == []

    async def test_no_wait_once_the_gap_has_elapsed(self):
        clock = Clock()
        limiter = make_limiter(clock)
        await limiter.acquire(f"{LIGHT}/a")
        clock.now += LIGHT_MIN_GAP  # time passed on its own
        await limiter.acquire(f"{LIGHT}/a")
        assert clock.sleeps == []

    async def test_untracked_paths_are_never_paced(self):
        clock = Clock()
        limiter = make_limiter(clock)
        await limiter.acquire("/clip/v2/resource/room/r")
        await limiter.acquire("/clip/v2/resource/room/r")
        assert clock.sleeps == []

    async def test_disabled_limiter_never_sleeps(self):
        clock = Clock()
        limiter = make_limiter(clock, enabled=False)
        await limiter.acquire(f"{LIGHT}/a")
        await limiter.acquire(f"{LIGHT}/a")
        assert clock.sleeps == []


class _RecordingResponse:
    def __init__(self) -> None:
        self.status = 200
        self.content_length = 1

    async def json(self) -> Any:
        return {"data": [], "errors": []}

    async def text(self) -> str:
        return ""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _RecordingSession:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def request(self, method: str, path: str, **_: Any) -> _RecordingResponse:
        del method
        self.paths.append(path)
        return _RecordingResponse()


class TestTransportIntegration:
    async def test_restore_style_fanout_is_throttled_through_the_transport(
        self, tmp_path
    ):
        # Proves the pacing applies on the real write path: a burst of per-light
        # PUTs (what ResourceGroup.restore fans out) is spaced by the transport.
        clock = Clock()
        config = HueConfig(
            bridge_ip="10.0.0.1",
            app_key="k",
            bridge_id="001788fffe25b8f8",
            config_path=tmp_path / "config.json",
        )
        client = HueHttpClient(config, rate_limiter=make_limiter(clock))
        session = _RecordingSession()
        client.session = cast("aiohttp.ClientSession", cast("object", session))

        lights = 5
        for index in range(lights):
            await client.put(f"{LIGHT}/{index}", {"on": {"on": True}})

        assert len(session.paths) == lights
        assert clock.sleeps == [pytest.approx(LIGHT_MIN_GAP)] * (lights - 1)
        assert clock.now == pytest.approx((lights - 1) * LIGHT_MIN_GAP)

    async def test_rate_limit_disabled_via_config_builds_a_noop_limiter(self, tmp_path):
        config = HueConfig(
            bridge_ip="10.0.0.1",
            app_key="k",
            bridge_id="001788fffe25b8f8",
            rate_limit=False,
            config_path=tmp_path / "config.json",
        )
        client = HueHttpClient(config)
        assert client._rate_limiter.enabled is False
