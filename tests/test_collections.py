"""Contract tests for the human-facing and typed API layers."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any, cast, override

import pytest

from huepy import (
    AmbiguousResourceError,
    CommandResult,
    Hue,
    ResourceNotFoundError,
    models,
)
from huepy.client.http import EventConnection, SSEFrame

from .conftest import FakeHttp

ROOM = "/clip/v2/resource/room"
GROUPED = "/clip/v2/resource/grouped_light"


def room(resource_id: str, name: str, grouped_light: str = "gl-1") -> dict[str, Any]:
    """Build a named room response."""
    return {
        "id": resource_id,
        "type": "room",
        "metadata": {"name": name},
        "services": [{"rid": grouped_light, "rtype": "grouped_light"}],
    }


class TestCanonicalVocabulary:
    def test_top_level_and_api_are_distinct_layers(self, hue):
        assert hue.rooms is not hue.api.rooms
        assert hasattr(hue.rooms, "names")
        assert not hasattr(hue.api.rooms, "names")
        assert not hasattr(hue.rooms, "__getitem__")
        assert not hasattr(hue.rooms, "by_name")
        assert not hasattr(hue.api.rooms, "get_all")
        assert not hasattr(hue.api.rooms, "all")

    def test_raw_transport_is_explicitly_under_api(self, hue, http):
        assert hue.api.raw is http


class TestOneShotCommands:
    async def test_room_set_resolves_once_then_delegates_to_bound_room(self, hue, http):
        http.queue_collection("room", [room("room-1", "Kitchen")])

        result = await hue.rooms.set(
            "Kitchen", brightness=40, kelvin=2200, transition=2
        )

        assert isinstance(result, CommandResult)
        assert result.sent is True
        assert [resource.rid for resource in result.resources] == ["updated-id"]
        assert http.calls == [
            ("GET", ROOM, None),
            (
                "PUT",
                f"{GROUPED}/gl-1",
                {
                    "dimming": {"brightness": 40},
                    "color_temperature": {"mirek": 455},
                    "dynamics": {"duration": 2000},
                },
            ),
        ]

    async def test_ambiguous_name_never_sends_a_mutation(self, hue, http):
        http.queue_collection(
            "room",
            [room("room-1", "Kitchen"), room("room-2", "kitchen", "gl-2")],
        )

        with pytest.raises(AmbiguousResourceError) as caught:
            await hue.rooms.turn_off("KITCHEN")

        assert caught.value.resource_ids == ["room-1", "room-2"]
        assert http.writes == []

    async def test_blank_name_never_matches_an_unnamed_resource(self, hue, http):
        http.queue_collection("room", [{"id": "room-1", "metadata": {"name": ""}}])

        with pytest.raises(ResourceNotFoundError):
            await hue.rooms.delete("  ")

        assert http.writes == []

    async def test_fetch_once_supports_repeated_commands_without_more_lookups(
        self, hue, http
    ):
        http.queue_collection("room", [room("room-1", "Kitchen")])
        kitchen = await hue.rooms.get("Kitchen")
        http.calls.clear()

        await kitchen.turn_on()
        await kitchen.set_brightness(60)

        assert [call[0] for call in http.calls] == ["PUT", "PUT"]


class TestCollectionCrud:
    async def test_create_returns_the_created_bound_resource(self, hue, http):
        http.write_result = {
            "errors": [],
            "data": [{"rid": "room-new", "rtype": "room"}],
        }
        http.queue_resource("room", "room-new", room("room-new", "Kitchen"))

        created = await hue.rooms.create("Kitchen", ["device-1"])

        assert isinstance(created, models.Room)
        assert created.is_bound
        assert http.calls == [
            (
                "POST",
                ROOM,
                {
                    "metadata": {"name": "Kitchen"},
                    "children": [{"rid": "device-1", "rtype": "device"}],
                },
            ),
            ("GET", f"{ROOM}/room-new", None),
        ]

    async def test_rename_and_delete_use_unique_name_resolution(self, hue, http):
        http.queue_collection("room", [room("room-1", "Kitchen")])
        await hue.rooms.rename("Kitchen", "North kitchen")
        assert http.last == (
            "PUT",
            f"{ROOM}/room-1",
            {"metadata": {"name": "North kitchen"}},
        )

        http.queue_collection("room", [room("room-1", "North kitchen")])
        await hue.rooms.delete("North kitchen")
        assert http.last == ("DELETE", f"{ROOM}/room-1", None)


class TestLiveResolver:
    async def test_live_collection_lookup_uses_the_local_graph(self, hue, http):
        local = models.Room.model_validate(room("room-1", "Kitchen")).bind(hue, "room")

        class Live:
            def ensure_healthy(self):
                return None

            def list(self, model: type[models.HueResource]):
                return [local] if model is models.Room else []

        hue._live_state = cast("Any", Live())

        found = await hue.rooms.get("Kitchen")

        assert found.id == "room-1"
        assert http.calls == []

    async def test_terminal_live_failure_is_raised_instead_of_serving_stale_data(
        self, hue
    ):
        state = hue.state()
        state._raw = {"room-1": room("room-1", "Kitchen")}
        state._terminal_error = RuntimeError("event observer stopped")
        hue._live_state = state

        with pytest.raises(RuntimeError, match="observer stopped"):
            await hue.rooms.get("Kitchen")

    def test_get_name_tracks_live_renames(self, hue):
        state = hue.state()
        state._raw = {"room-1": room("room-1", "Kitchen")}
        hue._live_state = state
        assert hue.get_name("room-1") == "Kitchen"

        state._raw["room-1"]["metadata"]["name"] = "North kitchen"

        assert hue.get_name("room-1") == "North kitchen"
        assert hue.names["room-1"] == "North kitchen"

    async def test_live_client_starts_with_one_snapshot_and_reuses_it(
        self, tmp_path, monkeypatch
    ):
        class LiveHttp(FakeHttp):
            def __init__(self) -> None:
                super().__init__()
                self.frames: asyncio.Queue[SSEFrame] = asyncio.Queue()

            @override
            async def event_connections(
                self, *, max_retries: int | None = 10
            ) -> AsyncGenerator[EventConnection]:
                del max_retries

                async def frame_stream() -> AsyncGenerator[SSEFrame]:
                    while True:
                        yield await self.frames.get()

                yield EventConnection(
                    opened_at=datetime.now(UTC),
                    resumed_from=None,
                    frames=frame_stream(),
                )

        http = LiveHttp()
        http.queue("/clip/v2/resource", {"data": [room("room-1", "Kitchen")]})
        monkeypatch.setattr("huepy.client.base.HueHttpClient", lambda _config: http)
        client = Hue(
            bridge_ip="10.0.0.1",
            app_key="key",
            config_path=tmp_path / "config.json",
            live=True,
        )

        async with client:
            assert http.calls == [("GET", "/clip/v2/resource", None)]
            assert (await client.rooms.get("Kitchen")).id == "room-1"
            assert http.calls == [("GET", "/clip/v2/resource", None)]
