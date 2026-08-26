"""Contract tests for handler registration on the state stream.

These pin the properties that make callbacks usable in a long-running process:
registration is gap-free, filters narrow correctly, and one bad handler cannot
take down the bus or its neighbours.
"""

import asyncio
from typing import Any

import pytest

from huepy import Hue, models
from huepy.state import Change, ChangeKind, Resync, ResyncReason

from .conftest import StateHttp
from .test_state import event_frame, light, update_frame


@pytest.fixture
def state_http(hue: Hue) -> StateHttp:
    http = StateHttp([[light(10)]])
    hue._http = http
    return http


async def settle() -> None:
    """Let the shared dispatcher drain what has been published."""
    for _ in range(5):
        await asyncio.sleep(0)


class TestRegistration:
    async def test_handler_registered_before_start_receives_changes(
        self, hue: Hue, state_http: StateHttp
    ) -> None:
        """Registering before start is the point of a permanent `hue.state`."""
        seen: list[Change] = []
        hue.state.on_change(seen.append)

        async with hue.state:
            await state_http.connections[0].put(update_frame(70))
            await settle()

        assert [
            c.after.brightness for c in seen if isinstance(c.after, models.Light)
        ] == [70]

    async def test_sync_and_async_handlers_both_run(
        self, hue: Hue, state_http: StateHttp
    ) -> None:
        """One alias covers both; dispatch awaits only what is awaitable."""
        seen: list[str] = []

        async def slow(_change: Change) -> None:
            seen.append("async")

        async with hue.state:
            hue.state.on_change(lambda _c: seen.append("sync"))
            hue.state.on_change(slow)
            await state_http.connections[0].put(update_frame(70))
            await settle()

        assert sorted(seen) == ["async", "sync"]

    async def test_cancel_stops_delivery(self, hue: Hue, state_http: StateHttp) -> None:
        seen: list[Change] = []
        async with hue.state:
            subscription = hue.state.on_change(seen.append)
            assert subscription.active is True
            subscription.cancel()
            assert subscription.active is False

            await state_http.connections[0].put(update_frame(70))
            await settle()

        assert seen == []

    async def test_subscription_scopes_to_a_block(
        self, hue: Hue, state_http: StateHttp
    ) -> None:
        seen: list[Change] = []
        async with hue.state:
            with hue.state.on_change(seen.append):
                await state_http.connections[0].put(update_frame(70))
                await settle()
            await state_http.connections[0].put(update_frame(80))
            await settle()

        assert len(seen) == 1


class TestFilters:
    async def test_name_filter_matches_case_insensitively(
        self, hue: Hue, state_http: StateHttp
    ) -> None:
        """`name=` is the ergonomic win: no id ever appears in caller code."""
        seen: list[Change] = []
        async with hue.state:
            hue.state.on_change(seen.append, name="  desk  ")
            hue.state.on_change(seen.append, name="Nothing here")
            await state_http.connections[0].put(update_frame(70))
            await settle()

        assert len(seen) == 1

    async def test_model_filter_matches_the_resource_type(
        self, hue: Hue, state_http: StateHttp
    ) -> None:
        seen: list[Change] = []
        async with hue.state:
            hue.state.on_change(seen.append, model=models.Light)
            hue.state.on_change(seen.append, model=models.Room)
            await state_http.connections[0].put(update_frame(70))
            await settle()

        assert len(seen) == 1

    async def test_filters_are_combined_with_and(
        self, hue: Hue, state_http: StateHttp
    ) -> None:
        seen: list[Change] = []
        async with hue.state:
            hue.state.on_change(seen.append, name="Desk", kind=ChangeKind.DELETE)
            await state_http.connections[0].put(update_frame(70))
            await settle()

        assert seen == []

    async def test_model_filter_still_matches_a_delete(
        self, hue: Hue, state_http: StateHttp
    ) -> None:
        """A delete has no `after`, so the filter must fall back to `before`."""
        seen: list[Change] = []
        async with hue.state:
            hue.state.on_change(seen.append, model=models.Light)
            await state_http.connections[0].put(
                event_frame("delete", {"id": "light-1", "type": "light"})
            )
            await settle()

        assert len(seen) == 1
        assert seen[0].kind is ChangeKind.DELETE


class TestFailureIsolation:
    async def test_a_raising_handler_does_not_stop_the_others(
        self, hue: Hue, state_http: StateHttp, caplog: Any
    ) -> None:
        """A process meant to run for weeks must survive one bad handler."""
        seen: list[Change] = []

        def broken(_change: Change) -> None:
            msg = "handler is broken"
            raise RuntimeError(msg)

        async with hue.state as state:
            hue.state.on_change(broken)
            hue.state.on_change(seen.append)

            await state_http.connections[0].put(update_frame(70))
            await settle()
            await state_http.connections[0].put(update_frame(80))
            await settle()

            assert state.tracking is True

        assert len(seen) == 2
        assert "handler is broken" in caplog.text


class TestResyncHandlers:
    async def test_markers_reach_on_resync_and_never_on_change(
        self, hue: Hue, state_http: StateHttp
    ) -> None:
        """Removing the mandatory isinstance guard is the whole point."""
        changes: list[Change] = []
        markers: list[Resync] = []

        async with hue.state:
            hue.state.on_change(changes.append)
            hue.state.on_resync(markers.append)

            state_http.connections.append(asyncio.Queue())
            await state_http.connections[0].put(None)
            for _ in range(50):
                await asyncio.sleep(0)
                if markers:
                    break

        assert markers
        assert markers[0].reason is ResyncReason.RECONNECT
        assert all(isinstance(item, Change) for item in changes)


class TestWatch:
    async def test_watch_yields_changes_only(
        self, hue: Hue, state_http: StateHttp, caplog: Any
    ) -> None:
        """No `isinstance` guard, but a discarded marker is never silent."""
        async with hue.state as state:
            stream = state.watch()

            state_http.connections.append(asyncio.Queue())
            await state_http.connections[0].put(None)
            await asyncio.sleep(0.05)
            await state_http.connections[1].put(update_frame(70))

            item = await asyncio.wait_for(anext(stream), 1)
            assert isinstance(item, Change)
            await stream.aclose()

        assert "discarded a reconnect marker" in caplog.text

    async def test_watch_filters_like_on_change(
        self, hue: Hue, state_http: StateHttp
    ) -> None:
        async with hue.state as state:
            stream = state.watch(name="Desk")
            await state_http.connections[0].put(update_frame(70))

            item = await asyncio.wait_for(anext(stream), 1)
            assert isinstance(item.after, models.Light)
            assert item.after.brightness == 70
            await stream.aclose()

    async def test_watch_registers_eagerly(
        self, hue: Hue, state_http: StateHttp
    ) -> None:
        """Same gap-free guarantee as `changes()`, for the same reason."""
        async with hue.state as state:
            stream = state.watch()

            await state_http.connections[0].put(update_frame(55))
            await asyncio.sleep(0.05)

            item = await asyncio.wait_for(anext(stream), 1)
            assert isinstance(item.after, models.Light)
            assert item.after.brightness == 55
            await stream.aclose()


class TestDescribe:
    async def test_describe_resolves_name_and_room(
        self, hue: Hue, state_http: StateHttp
    ) -> None:
        async with hue.state as state:
            stream = state.watch()
            await state_http.connections[0].put(update_frame(70))
            change = await asyncio.wait_for(anext(stream), 1)

            context = state.describe(change)
            assert context.change is change
            assert context.name == "Desk"
            assert context.room is None
            await stream.aclose()


class TestTerminalFailure:
    async def test_a_terminal_stream_error_is_logged_not_silent(
        self, hue: Hue, state_http: StateHttp, caplog: Any
    ) -> None:
        """Handlers cannot see the broadcast exception, so it must be logged."""
        seen: list[Change] = []
        state = hue.state
        state.on_change(seen.append)

        async with state:
            await state._broadcast(RuntimeError("event observer stopped"))
            await settle()

        assert "handlers will receive nothing" in caplog.text

    async def test_close_completes_despite_a_terminal_dispatch_error(
        self, hue: Hue, state_http: StateHttp
    ) -> None:
        """A dead dispatch task must not abandon the rest of shutdown."""
        state = hue.state
        state.on_change(lambda _c: None)
        await state.__aenter__()
        await state._broadcast(RuntimeError("event observer stopped"))
        await settle()

        await state.close()

        assert state.tracking is False
        assert state._dispatch_task is None


DEVICE = {
    "id": "device-1",
    "type": "device",
    "metadata": {"name": "Desk device"},
    "services": [{"rid": "light-1", "rtype": "light"}],
}
OTHER_DEVICE = {
    "id": "device-2",
    "type": "device",
    "metadata": {"name": "Hob device"},
    "services": [{"rid": "light-2", "rtype": "light"}],
}
ROOM = {
    "id": "room-1",
    "type": "room",
    "metadata": {"name": "Office"},
    "children": [{"rid": "device-1", "rtype": "device"}],
}
OTHER_ROOM = {
    "id": "room-2",
    "type": "room",
    "metadata": {"name": "Kitchen"},
    "children": [{"rid": "device-2", "rtype": "device"}],
}
OTHER_LIGHT = {
    "id": "light-2",
    "type": "light",
    "owner": {"rid": "device-2", "rtype": "device"},
    "metadata": {"name": "Hob"},
    "on": {"on": True},
    "dimming": {"brightness": 10},
}


@pytest.fixture
def rooms_http(hue: Hue) -> StateHttp:
    """Two rooms, one light each, so a room filter has something to exclude."""
    http = StateHttp([[light(10), DEVICE, ROOM, OTHER_DEVICE, OTHER_ROOM, OTHER_LIGHT]])
    hue._http = http
    return http


def other_light_frame(brightness: float) -> Any:
    return update_frame(
        brightness,
        entries=[
            {"id": "light-2", "type": "light", "dimming": {"brightness": brightness}}
        ],
    )


class TestRoomFilter:
    async def test_only_changes_in_that_room_arrive(
        self, hue: Hue, rooms_http: StateHttp
    ) -> None:
        """Resolved through the owning device: a light's own owner is not a room."""
        seen: list[Change] = []
        async with hue.state:
            hue.state.on_change(seen.append, room="Office")
            await rooms_http.connections[0].put(update_frame(70))
            await rooms_http.connections[0].put(other_light_frame(80))
            await settle()

        assert [c.resource_id for c in seen] == ["light-1"]

    async def test_the_room_name_is_matched_case_insensitively(
        self, hue: Hue, rooms_http: StateHttp
    ) -> None:
        seen: list[Change] = []
        async with hue.state:
            hue.state.on_change(seen.append, room="  office ")
            await rooms_http.connections[0].put(update_frame(70))
            await settle()

        assert len(seen) == 1

    async def test_a_room_filter_still_matches_a_delete(
        self, hue: Hue, rooms_http: StateHttp
    ) -> None:
        """The light has left the graph, so the room resolves via what it carried."""
        seen: list[Change] = []
        async with hue.state:
            hue.state.on_change(seen.append, room="Office")
            await rooms_http.connections[0].put(
                event_frame("delete", {"id": "light-1", "type": "light"})
            )
            await settle()

        assert [c.kind for c in seen] == [ChangeKind.DELETE]

    async def test_room_and_name_filters_are_anded(
        self, hue: Hue, rooms_http: StateHttp
    ) -> None:
        seen: list[Change] = []
        async with hue.state:
            hue.state.on_change(seen.append, name="Desk", room="Kitchen")
            await rooms_http.connections[0].put(update_frame(70))
            await settle()

        assert seen == []

    async def test_watch_filters_by_room_too(
        self, hue: Hue, rooms_http: StateHttp
    ) -> None:
        async with hue.state as state:
            stream = state.watch(room="Office")
            await rooms_http.connections[0].put(other_light_frame(80))
            await rooms_http.connections[0].put(update_frame(70))
            change = await asyncio.wait_for(anext(stream), 1)

            assert change.resource_id == "light-1"
            await stream.aclose()


class TestWaitFor:
    async def test_returns_the_first_matching_change(
        self, hue: Hue, state_http: StateHttp
    ) -> None:
        async with hue.state as state:
            waiting = asyncio.ensure_future(state.wait_for(name="Desk"))
            await settle()
            await state_http.connections[0].put(update_frame(70))

            change = await asyncio.wait_for(waiting, 1)

        assert change.resource_id == "light-1"

    async def test_a_predicate_narrows_past_the_filters(
        self, hue: Hue, state_http: StateHttp
    ) -> None:
        """Filters cannot express "brightness above 50"; a predicate can."""
        async with hue.state as state:
            waiting = asyncio.ensure_future(
                state.wait_for(
                    predicate=lambda c: c.delta["dimming"]["brightness"] > 50
                )
            )
            await settle()
            await state_http.connections[0].put(update_frame(20))
            await state_http.connections[0].put(update_frame(70))

            change = await asyncio.wait_for(waiting, 1)

        assert change.delta["dimming"]["brightness"] == 70

    async def test_the_subscriber_is_registered_before_the_first_await(
        self, hue: Hue, state_http: StateHttp
    ) -> None:
        """Otherwise a change caused by the write you just issued is missed."""
        async with hue.state as state:
            waiting = asyncio.ensure_future(state.wait_for())
            await settle()

            assert len(state._subscribers) == 1
            await state_http.connections[0].put(update_frame(70))
            await asyncio.wait_for(waiting, 1)

    async def test_a_timeout_raises_and_leaves_no_subscriber_behind(
        self, hue: Hue, state_http: StateHttp
    ) -> None:
        async with hue.state as state:
            with pytest.raises(TimeoutError):
                await state.wait_for(name="Nothing", timeout=0.01)
            await settle()

            assert state._subscribers == set()

    async def test_a_non_matching_change_does_not_end_the_wait(
        self, hue: Hue, rooms_http: StateHttp
    ) -> None:
        async with hue.state as state:
            waiting = asyncio.ensure_future(state.wait_for(room="Office", timeout=1))
            await settle()
            await rooms_http.connections[0].put(other_light_frame(80))
            await settle()
            assert not waiting.done()

            await rooms_http.connections[0].put(update_frame(70))
            change = await asyncio.wait_for(waiting, 1)

        assert change.resource_id == "light-1"


class TestDeletes:
    async def test_a_name_filter_still_matches_a_delete(
        self, hue: Hue, state_http: StateHttp
    ) -> None:
        """The resource is folded out before dispatch, so the live lookup misses.

        Watching a light by name and never being told it was removed is the
        worst possible failure for a name filter.
        """
        seen: list[Change] = []
        async with hue.state:
            hue.state.on_change(seen.append, name="Desk")
            await state_http.connections[0].put(
                event_frame("delete", {"id": "light-1", "type": "light"})
            )
            await settle()

        assert len(seen) == 1
        assert seen[0].kind is ChangeKind.DELETE

    async def test_describe_keeps_the_name_of_a_deleted_resource(
        self, hue: Hue, state_http: StateHttp
    ) -> None:
        """Otherwise every delete row lands in history as name='Unknown'."""
        async with hue.state as state:
            stream = state.watch()
            await state_http.connections[0].put(
                event_frame("delete", {"id": "light-1", "type": "light"})
            )
            change = await asyncio.wait_for(anext(stream), 1)

            assert state.describe(change).name == "Desk"
            await stream.aclose()


class TestDispatchLifetime:
    async def test_cancelling_the_last_handler_stops_the_shared_reader(
        self, hue: Hue, state_http: StateHttp
    ) -> None:
        """Left running it deep-copies every change into a queue nobody reads."""
        async with hue.state as state:
            before = len(state._subscribers)
            subscription = state.on_change(lambda _c: None)
            await settle()
            assert len(state._subscribers) == before + 1

            subscription.cancel()
            await settle()

            assert state._dispatch_task is None
            assert len(state._subscribers) == before

    async def test_a_remaining_handler_keeps_the_reader_alive(
        self, hue: Hue, state_http: StateHttp
    ) -> None:
        seen: list[Change] = []
        async with hue.state as state:
            first = state.on_change(lambda _c: None)
            state.on_change(seen.append)
            first.cancel()
            await settle()

            assert state._dispatch_task is not None
            await state_http.connections[0].put(update_frame(70))
            await settle()

        assert len(seen) == 1


class TestSelfCancellation:
    async def test_an_async_handler_can_cancel_its_own_subscription(
        self, hue: Hue, state_http: StateHttp
    ) -> None:
        """The one-shot idiom: the handler runs *inside* the dispatch task.

        Cancelling that task from within would deliver the CancelledError into
        the handler at its next await, tearing it apart mid-body.
        """
        seen: list[Change] = []
        finished = asyncio.Event()

        async def once(item: Change) -> None:
            subscription.cancel()
            await asyncio.sleep(0)  # the await that used to be interrupted
            seen.append(item)
            finished.set()

        async with hue.state as state:
            subscription = state.on_change(once)
            await state_http.connections[0].put(update_frame(70))
            await asyncio.wait_for(finished.wait(), 1)

            await state_http.connections[0].put(update_frame(80))
            await settle()

            assert state._dispatch_task is None

        assert len(seen) == 1
