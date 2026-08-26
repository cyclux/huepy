"""Contract tests for the human-facing and typed API layers."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any, cast, override

import pytest

from huepy import (
    AmbiguousResourceError,
    BridgeConnectionError,
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
    @pytest.mark.parametrize(
        "attribute", ["rooms", "zones", "scenes", "service_groups"]
    )
    def test_wire_shaped_creation_stays_on_the_low_level_api(self, hue, attribute):
        assert not hasattr(getattr(hue, attribute), "create")
        assert hasattr(getattr(hue.api, attribute), "create")

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


SMART_SCENE = "/clip/v2/resource/smart_scene"


class TestSmartSceneCollection:
    """A smart scene is addressed by the name a human gave it."""

    @staticmethod
    def _smart_scene(name: str = "Rhythm", scene_id: str = "ss-1") -> dict[str, Any]:
        return {
            "id": scene_id,
            "type": "smart_scene",
            "metadata": {"name": name},
            "week_timeslots": [],
        }

    async def test_activate_resolves_by_name_then_recalls(self, hue, http):
        http.queue_collection("smart_scene", [self._smart_scene()])

        await hue.smart_scenes.activate("rhythm")

        assert http.last == (
            "PUT",
            f"{SMART_SCENE}/ss-1",
            {"recall": {"action": "activate"}},
        )

    async def test_deactivate_resolves_by_name_then_recalls(self, hue, http):
        http.queue_collection("smart_scene", [self._smart_scene()])

        await hue.smart_scenes.deactivate("Rhythm")

        assert http.last == (
            "PUT",
            f"{SMART_SCENE}/ss-1",
            {"recall": {"action": "deactivate"}},
        )


def track(hue: Hue, raw: dict[str, Any], *, connected: bool = True):
    """Make ``hue.state`` report as tracking ``raw``, with no event stream.

    ``tracking`` asks whether an observer task exists; these tests drive the
    graph directly instead of running one, so the task is only ever compared
    against None.
    """
    state = hue.state
    state._raw = raw
    state._connected = connected
    state._started = True
    state._task = cast("Any", _OBSERVING)
    return state


_OBSERVING = object()


class TestTrackedResolver:
    async def test_tracked_collection_lookup_uses_the_local_graph(self, hue, http):
        local = models.Room.model_validate(room("room-1", "Kitchen")).bind(hue, "room")

        class Tracked:
            tracking = True

            def ensure_resolver_healthy(self):
                return None

            def list(self, model: type[models.HueResource]):
                return [local] if model is models.Room else []

        hue._state = cast("Any", Tracked())

        found = await hue.rooms.get("Kitchen")

        assert found.id == "room-1"
        assert http.calls == []

    async def test_terminal_failure_is_raised_instead_of_serving_stale_data(self, hue):
        state = track(hue, {"room-1": room("room-1", "Kitchen")})
        state._terminal_error = RuntimeError("event observer stopped")

        with pytest.raises(RuntimeError, match="observer stopped"):
            await hue.rooms.get("Kitchen")

    async def test_transient_disconnect_prevents_stale_name_mutation(self, hue, http):
        track(hue, {"room-1": room("room-1", "Kitchen")}, connected=False)

        with pytest.raises(BridgeConnectionError, match="reconnecting"):
            await hue.rooms.delete("Kitchen")

        assert http.writes == []

    async def test_untracked_state_falls_back_to_the_bridge(self, hue, http):
        """A constructed but unstarted graph must not shadow the bridge."""
        http.queue_collection("room", [room("room-1", "Kitchen")])

        assert (await hue.rooms.get("Kitchen")).id == "room-1"
        assert http.calls == [("GET", ROOM, None)]

    async def test_get_name_tracks_renames(self, hue):
        """A rename arrives as an event, so folding one is the real path.

        The name map is memoised per graph revision; poking `_raw` directly
        would skip the invalidation a genuine rename goes through and prove
        nothing about it.
        """
        state = track(hue, {"room-1": room("room-1", "Kitchen")})
        assert hue.get_name("room-1") == "Kitchen"

        await state._fold_frame(
            state._raw,
            SSEFrame(
                event_id="1:1",
                received_at=datetime.now(UTC),
                events=[
                    {
                        "id": "event-1",
                        "type": "update",
                        "creationtime": "2026-08-24T10:00:00Z",
                        "data": [
                            {
                                "id": "room-1",
                                "type": "room",
                                "metadata": {"name": "North kitchen"},
                            }
                        ],
                    }
                ],
            ),
            publish=False,
        )

        assert hue.get_name("room-1") == "North kitchen"
        assert hue.names["room-1"] == "North kitchen"

    async def test_tracking_client_starts_with_one_snapshot_and_reuses_it(
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
            state=True,
        )

        async with client:
            assert http.calls == [("GET", "/clip/v2/resource", None)]
            assert (await client.rooms.get("Kitchen")).id == "room-1"
            assert http.calls == [("GET", "/clip/v2/resource", None)]
