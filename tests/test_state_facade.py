"""Contract tests for `hue.state` as a permanent client attribute.

The graph is reachable from construction so handlers and sinks can register
before the stream opens. These tests pin the two properties that makes safe:
attribute access never raises, and a graph that has never observed refuses to
answer instead of reporting an empty bridge.
"""

import asyncio
import sqlite3
from typing import Any

import pytest

from huepy import Hue, StateNotStartedError, models
from huepy.recording import SQLiteSink
from huepy.state import HueState

from .conftest import FakeHttp, StateHttp
from .test_state import update_frame


def test_state_is_reachable_before_the_client_starts(bare_hue: Hue) -> None:
    """`hasattr` must not raise -- the doc gate probes an unstarted client."""
    assert hasattr(bare_hue, "state")
    assert isinstance(bare_hue.state, HueState)
    assert bare_hue.state.tracking is False


def test_state_identity_is_stable(hue: Hue) -> None:
    """Registrations made before start must survive to the running graph."""
    assert hue.state is hue.state


def test_state_is_not_optional(hue: Hue) -> None:
    """The old `live_state` was `HueState | None`; this one never is."""
    assert not hasattr(hue, "live_state")


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(lambda state: state.resources, id="resources"),
        pytest.param(lambda state: state.by_id("light-1"), id="by_id"),
        pytest.param(lambda state: state.list(models.Light), id="list"),
        pytest.param(lambda state: state.lights.list(), id="view-list"),
        pytest.param(lambda state: state.lights.names(), id="view-names"),
        pytest.param(lambda state: state.name_of("light-1"), id="name_of"),
        pytest.param(lambda state: state.room_of("light-1"), id="room_of"),
    ],
)
def test_reads_before_tracking_refuse_instead_of_answering_empty(
    hue: Hue,
    read: Any,
) -> None:
    """An empty graph would report "no lights" for "not tracking yet"."""
    with pytest.raises(StateNotStartedError, match="not being tracked"):
        read(hue.state)


def test_view_get_reports_not_tracking_rather_than_a_missing_name(hue: Hue) -> None:
    """`ResourceNotFoundError` here would send the caller hunting a typo."""
    with pytest.raises(StateNotStartedError):
        hue.state.lights.get("Desk lamp")


async def test_untracked_client_makes_no_background_requests(
    http: FakeHttp,
    tmp_path,
    monkeypatch,
) -> None:
    """The opt-in default: a plain client costs nothing in the background."""
    monkeypatch.setattr("huepy.client.base.HueHttpClient", lambda _config: http)
    client = Hue(
        bridge_ip="10.0.0.1", app_key="k", config_path=tmp_path / "config.json"
    )

    async with client:
        assert client.state.tracking is False

    assert http.calls == []


def _light(name: str) -> dict[str, Any]:
    """One minimally valid light resource."""
    return {
        "id": "light-1",
        "type": "light",
        "metadata": {"name": name},
        "on": {"on": True},
    }


async def test_reentering_serves_the_new_snapshot(tmp_path, monkeypatch) -> None:
    """The graph outlives close(), so a restart must not serve the old one."""
    http = StateHttp([[_light("Desk")], [_light("Bench")]])
    monkeypatch.setattr("huepy.client.base.HueHttpClient", lambda _config: http)
    client = Hue(
        bridge_ip="10.0.0.1", app_key="k", config_path=tmp_path / "config.json"
    )
    client._http = http

    async with client.state as state:
        assert state.lights.get("Desk").id == "light-1"

    async with client.state as state:
        assert state.name_of("light-1") == "Bench"
        assert state.lights.names() == ["Bench"]


async def test_state_true_starts_and_stops_tracking(
    tmp_path,
    monkeypatch,
) -> None:
    """`state=True` is the whole opt-in, and close() stops observation."""
    http = StateHttp([[]])
    monkeypatch.setattr("huepy.client.base.HueHttpClient", lambda _config: http)
    client = Hue(
        bridge_ip="10.0.0.1",
        app_key="k",
        config_path=tmp_path / "config.json",
        state=True,
    )

    async with client:
        assert client.state.tracking is True
        assert client.state.resources == []

    assert client.state.tracking is False
    # A closed graph stops observing but keeps what it observed; refusing to
    # read it back would regress a view that was always last-reported state.
    assert client.state.resources == []


async def test_record_implies_state_and_persists_through_the_client(
    tmp_path,
    monkeypatch,
) -> None:
    """End to end: one constructor argument, and the history is on disk."""
    database = tmp_path / "history.sqlite3"
    http = StateHttp([[_light("Desk")]])
    monkeypatch.setattr("huepy.client.base.HueHttpClient", lambda _config: http)
    client = Hue(
        bridge_ip="10.0.0.1",
        app_key="k",
        config_path=tmp_path / "config.json",
        record=SQLiteSink(database),
    )

    async with client:
        # `record=` alone switched tracking on; a second flag would be a trap.
        assert client.state.tracking is True
        assert client.recorder is not None
        await http.connections[0].put(update_frame(60))
        await asyncio.sleep(0.05)

    connection = sqlite3.connect(database)
    try:
        rows = connection.execute("SELECT name, brightness FROM change").fetchall()
        current = connection.execute("SELECT brightness FROM current").fetchall()
    finally:
        connection.close()

    assert rows == [("Desk", 60.0)]
    assert current == [(60.0,)]


async def test_a_recording_client_can_be_restarted(tmp_path, monkeypatch) -> None:
    """The sinks' worker threads must not make `record=` single-use."""
    database = tmp_path / "history.sqlite3"
    http = StateHttp([[_light("Desk")], [_light("Desk")]])
    monkeypatch.setattr("huepy.client.base.HueHttpClient", lambda _config: http)
    client = Hue(
        bridge_ip="10.0.0.1",
        app_key="k",
        config_path=tmp_path / "config.json",
        record=SQLiteSink(database),
    )

    for brightness in (60, 80):
        async with client:
            await http.connections[0].put(update_frame(brightness))
            await asyncio.sleep(0.05)
        http.connections = [asyncio.Queue()]

    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT brightness FROM change ORDER BY id"
        ).fetchall()
    finally:
        connection.close()

    assert rows == [(60.0,), (80.0,)]
