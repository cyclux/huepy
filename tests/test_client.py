"""Tests for the Hue client: lifecycle, name lookup, authentication guard."""

import logging
from typing import Any, cast

import pytest

from huepy import Hue
from huepy.exceptions import AuthenticationError
from huepy.models.event import HueEvent


class TestLifecycle:
    async def test_http_before_start_raises(self, bare_hue):
        with pytest.raises(RuntimeError, match="Client not initialized"):
            _ = bare_hue.http

    async def test_close_without_start_is_a_no_op(self, bare_hue):
        await bare_hue.close()

    async def test_close_clears_the_transport(self, hue, http):
        await hue.close()
        with pytest.raises(RuntimeError, match="Client not initialized"):
            _ = hue.http

    async def test_close_releases_all_resources_before_surfacing_an_error(
        self, hue, http
    ):
        class Stream:
            def __init__(self, error: BaseException | None = None) -> None:
                self.error = error
                self.closed = False

            async def aclose(self) -> None:
                self.closed = True
                if self.error is not None:
                    raise self.error

        failing = Stream(RuntimeError("already running"))
        healthy = Stream()
        hue._event_streams.update(
            {
                cast("Any", failing),
                cast("Any", healthy),
            }
        )

        with pytest.raises(RuntimeError, match="already running"):
            await hue.close()

        assert failing.closed is True
        assert healthy.closed is True
        assert http.closed is True
        assert hue._http is None
        assert not hue._event_streams

    async def test_context_manager_starts_and_closes(self, tmp_path, monkeypatch):
        started: list[str] = []

        async def fake_start(self) -> None:
            started.append("start")

        async def fake_close(self) -> None:
            started.append("close")

        monkeypatch.setattr(Hue, "start", fake_start)
        monkeypatch.setattr(Hue, "close", fake_close)

        async with Hue(bridge_ip="10.0.0.1", config_path=tmp_path / "c.json") as client:
            assert isinstance(client, Hue)

        assert started == ["start", "close"]


class TestNames:
    async def test_refresh_names_merges_devices_lights_and_rooms(self, hue, http):
        http.queue_collection(
            "device", [{"id": "dev-1", "metadata": {"name": "Ceiling"}}]
        )
        http.queue_collection(
            "light",
            [
                {
                    "id": "svc-1",
                    "metadata": {"name": "Desk"},
                    "owner": {"rid": "dev-2", "rtype": "device"},
                }
            ],
        )
        http.queue_collection(
            "room", [{"id": "room-1", "metadata": {"name": "Kitchen"}}]
        )

        http.queue(
            "/clip/v2/resource",
            {
                "data": [
                    {"id": "dev-1", "type": "device", "metadata": {"name": "Ceiling"}},
                    {
                        "id": "svc-1",
                        "type": "light",
                        "metadata": {"name": "Desk"},
                        "owner": {"rid": "dev-2", "rtype": "device"},
                    },
                    {"id": "room-1", "type": "room", "metadata": {"name": "Kitchen"}},
                ]
            },
        )
        names = await hue.refresh_names()

        assert names == {
            "dev-1": "Ceiling",
            "svc-1": "Desk",
            "dev-2": "Desk",
            "room-1": "Kitchen",
        }

    async def test_refresh_names_uses_one_aggregate_request(self, hue, http):
        http.queue("/clip/v2/resource", {"data": []})
        await hue.refresh_names()
        assert http.paths == ["/clip/v2/resource"]

    async def test_get_name_resolves_zones_and_scenes(self, hue, http):
        http.queue(
            "/clip/v2/resource",
            {
                "data": [
                    {"id": "z-1", "type": "zone", "metadata": {"name": "Downstairs"}},
                    {"id": "s-1", "type": "scene", "metadata": {"name": "Relax"}},
                ]
            },
        )
        await hue.refresh_names()
        assert hue.get_name("z-1") == "Downstairs"
        assert hue.get_name("s-1") == "Relax"

    async def test_event_stream_yields_parsed_models(self, hue, http):
        http.events = [
            {
                "id": "evt-1",
                "type": "update",
                "creationtime": "2026-08-22T10:00:00Z",
                "data": [
                    {"id": "light-1", "type": "light", "on": {"on": True}},
                ],
            }
        ]
        received = [event async for event in hue.get_event_stream()]
        assert len(received) == 1
        event = received[0]
        assert isinstance(event, HueEvent)
        assert event.is_update
        assert event.resource_ids == ["light-1"]
        assert event.data[0].on is not None
        assert event.data[0].on.on is True

    async def test_event_stream_survives_an_unparseable_event(self, hue, http, caplog):
        """One malformed event must not end a stream meant to run for weeks."""
        http.events = [
            {"id": "bad", "type": "update", "data": "not-a-list"},
            {"id": "good", "type": "update", "data": [{"id": "light-1"}]},
        ]
        with caplog.at_level(logging.WARNING):
            received = [event async for event in hue.get_event_stream()]
        assert [event.id for event in received] == ["good"]
        assert "Discarding unparseable event" in caplog.text

    async def test_get_name_returns_unknown_for_missing_ids(self, hue):
        assert hue.get_name("nope") == "Unknown"

    async def test_get_name_after_refresh(self, hue, http):
        http.queue(
            "/clip/v2/resource",
            {
                "data": [
                    {"id": "dev-1", "type": "device", "metadata": {"name": "Ceiling"}}
                ]
            },
        )
        await hue.refresh_names()
        assert hue.get_name("dev-1") == "Ceiling"

    async def test_names_property_exposes_the_lookup(self, hue, http):
        http.queue(
            "/clip/v2/resource",
            {
                "data": [
                    {"id": "dev-1", "type": "device", "metadata": {"name": "Ceiling"}}
                ]
            },
        )
        await hue.refresh_names()
        assert hue.names["dev-1"] == "Ceiling"


class TestAuthenticationGuard:
    async def test_passes_when_a_key_is_present(self, hue):
        hue.ensure_authenticated()

    async def test_raises_with_an_actionable_message_when_absent(self, tmp_path):
        """The library must not block on input(); it explains what to do instead."""
        client = Hue(bridge_ip="10.0.0.1", config_path=tmp_path / "missing.json")
        with pytest.raises(AuthenticationError) as excinfo:
            client.ensure_authenticated()
        message = str(excinfo.value)
        assert "link button" in message
        assert "authenticate()" in message
        assert str(tmp_path / "missing.json") in message

    async def test_event_stream_requires_a_key(self, tmp_path):
        client = Hue(bridge_ip="10.0.0.1", config_path=tmp_path / "missing.json")
        with pytest.raises(AuthenticationError):
            await anext(client.get_event_stream())


class TestHandlerWiring:
    @pytest.mark.parametrize(
        "attribute",
        [
            "lights",
            "grouped_lights",
            "light_levels",
            "grouped_light_levels",
            "rooms",
            "zones",
            "scenes",
            "devices",
            "device_powers",
            "bridges",
            "bridge_homes",
            "service_groups",
            "motions",
            "grouped_motions",
            "temperatures",
            "buttons",
            "contacts",
        ],
    )
    async def test_every_documented_handler_exists(self, hue, attribute):
        assert getattr(hue.api, attribute) is not None

    async def test_handlers_share_the_client(self, hue):
        assert hue.api.lights.hue is hue
        assert hue.api.rooms.hue is hue


class TestLazyStart:
    """Opening a stateless client performs no resource I/O."""

    async def test_transport_is_closed_when_refresh_fails(self, tmp_path, monkeypatch):
        closed: list[str] = []

        class DyingHttp:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc: object) -> None:
                closed.append("closed")

            async def get(self, path: str):
                msg = "bridge unreachable"
                raise ConnectionError(msg)

        monkeypatch.setattr(
            "huepy.client.base.HueHttpClient", lambda _config: DyingHttp()
        )
        client = Hue(bridge_ip="10.0.0.1", app_key="k", config_path=tmp_path / "c.json")

        await client.start()
        assert closed == []
        await client.close()
        assert closed == ["closed"]

    async def test_context_manager_also_cleans_up(self, tmp_path, monkeypatch):
        closed: list[str] = []

        class DyingHttp:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc: object) -> None:
                closed.append("closed")

            async def get(self, path: str):
                msg = "bridge unreachable"
                raise ConnectionError(msg)

        monkeypatch.setattr(
            "huepy.client.base.HueHttpClient", lambda _config: DyingHttp()
        )

        async with Hue(
            bridge_ip="10.0.0.1", app_key="k", config_path=tmp_path / "c.json"
        ):
            pass

        assert closed == ["closed"]


class TestStartWithoutAKey:
    """start() must stay usable on a bridge that has not issued a key yet.

    Regression: start() unconditionally called refresh_names(), whose five
    requests a keyless bridge rejects -- so authenticate(), the one call that
    obtains a key, could not be reached through the client at all.
    """

    async def test_start_skips_the_name_lookup_without_a_key(
        self, tmp_path, monkeypatch
    ):
        requested: list[str] = []

        class SilentHttp:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc: object) -> None:
                return None

            async def get(self, path: str):
                requested.append(path)
                return {"data": []}

            async def authenticate(
                self,
                app_name: str = "huepy",
                timeout: int = 60,  # noqa: ASYNC109 - mirrors the Transport protocol
            ):
                return "issued-key"

        monkeypatch.setattr(
            "huepy.client.base.HueHttpClient", lambda _config: SilentHttp()
        )
        client = Hue(bridge_ip="10.0.0.1", config_path=tmp_path / "c.json")

        async with client:
            assert requested == [], "a keyless bridge must not be queried"
            assert await client.authenticate() == "issued-key"

    async def test_live_start_without_a_key_fails_and_closes_transport(
        self, tmp_path, monkeypatch
    ):
        closed = False

        class SilentHttp:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc: object) -> None:
                nonlocal closed
                closed = True

        monkeypatch.setattr(
            "huepy.client.base.HueHttpClient", lambda _config: SilentHttp()
        )
        client = Hue(
            bridge_ip="10.0.0.1",
            config_path=tmp_path / "c.json",
            state=True,
        )

        with pytest.raises(AuthenticationError, match="No Hue application key"):
            await client.start()

        assert closed is True
        assert client._http is None

    async def test_stateless_start_skips_lookup_even_with_a_key(
        self, tmp_path, monkeypatch
    ):
        requested: list[str] = []

        class SilentHttp:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc: object) -> None:
                return None

            async def get(self, path: str):
                requested.append(path)
                return {"data": []}

        monkeypatch.setattr(
            "huepy.client.base.HueHttpClient", lambda _config: SilentHttp()
        )
        client = Hue(bridge_ip="10.0.0.1", app_key="k", config_path=tmp_path / "c.json")

        await client.start()
        assert requested == []
        assert client.get_name("anything") == "Unknown"
        await client.close()


class TestEventStreamCleanup:
    """Breaking out of the stream must release its response, not leak it.

    Regression: a live run logged ResourceWarning for an unclosed transport
    after a caller broke out of the event stream.
    """

    async def test_close_finalises_an_abandoned_stream(self, hue, http):
        http.events = [{"data": [{"id": "a", "type": "light"}]} for _ in range(5)]

        async for _event in hue.get_event_stream():
            break  # abandon it mid-iteration

        assert hue._event_streams, "stream should still be tracked"
        await hue.close()
        assert not hue._event_streams, "close() should finalise the stream"

    async def test_exhausting_the_stream_clears_the_reference(self, hue, http):
        http.events = [{"data": []}]
        received = [event async for event in hue.get_event_stream()]
        assert len(received) == 1
        assert not hue._event_streams
