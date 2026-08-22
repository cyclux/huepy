"""Tests for the id-to-display-name lookup."""

from huepy import models
from huepy.utils.naming import build_name_map


def light(resource_id: str, name: str, owner: str | None = None) -> models.Light:
    payload = {"id": resource_id, "metadata": {"name": name}}
    if owner is not None:
        payload["owner"] = {"rid": owner, "rtype": "device"}
    return models.Light.model_validate(payload)


def device(resource_id: str, name: str) -> models.Device:
    return models.Device.model_validate({"id": resource_id, "metadata": {"name": name}})


def test_empty_input():
    assert build_name_map() == {}
    assert build_name_map([], []) == {}


def test_maps_a_resource_by_its_own_id():
    assert build_name_map([device("dev-1", "Ceiling")]) == {"dev-1": "Ceiling"}


def test_a_service_also_maps_its_owning_device():
    """A light appears under both its service id and its device id."""
    assert build_name_map([light("svc-1", "Desk", owner="dev-1")]) == {
        "svc-1": "Desk",
        "dev-1": "Desk",
    }


def test_unnamed_resources_are_skipped():
    assert build_name_map([device("dev-1", "")]) == {}


def test_later_groups_win_on_collision():
    result = build_name_map([device("dev-1", "Old")], [device("dev-1", "New")])
    assert result == {"dev-1": "New"}


def test_merges_across_groups():
    result = build_name_map(
        [device("dev-1", "Ceiling")],
        [light("svc-2", "Desk")],
    )
    assert result == {"dev-1": "Ceiling", "svc-2": "Desk"}


class TestContainedServicesInheritTheirContainerName:
    """A room's grouped_light and a switch's buttons have no name of their own.

    The event stream is largely made of those service ids, so without this
    most events resolved to "Unknown" -- verified against a live bridge, where
    it took the name map from 74 entries to 163.
    """

    def test_room_services_resolve_to_the_room_name(self):
        room = models.Room.model_validate(
            {
                "id": "r1",
                "metadata": {"name": "Kitchen"},
                "services": [{"rid": "gl1", "rtype": "grouped_light"}],
            }
        )
        names = build_name_map([room])
        assert names["gl1"] == "Kitchen"
        assert names["r1"] == "Kitchen"

    def test_device_services_resolve_to_the_device_name(self):
        device = models.Device.model_validate(
            {
                "id": "d1",
                "metadata": {"name": "Dimmer Switch"},
                "services": [
                    {"rid": "b1", "rtype": "button"},
                    {"rid": "b2", "rtype": "button"},
                ],
            }
        )
        names = build_name_map([device])
        assert names["b1"] == names["b2"] == "Dimmer Switch"

    def test_a_service_with_its_own_name_keeps_it(self):
        """An inherited name must never overwrite a real one."""
        light = models.Light.model_validate(
            {"id": "svc1", "metadata": {"name": "Desk Lamp"}}
        )
        room = models.Room.model_validate(
            {
                "id": "r1",
                "metadata": {"name": "Kitchen"},
                "services": [{"rid": "svc1", "rtype": "light"}],
            }
        )
        assert build_name_map([light], [room])["svc1"] == "Desk Lamp"
        assert build_name_map([room], [light])["svc1"] == "Desk Lamp"
