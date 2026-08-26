"""Typed, id-addressed access to the Hue CLIP v2 resource API."""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from huepy.resources import (
    BehaviorInstance,
    BehaviorScript,
    Bridge,
    BridgeHome,
    Button,
    CameraMotion,
    Contact,
    Device,
    DevicePower,
    DeviceSoftwareUpdate,
    Entertainment,
    EntertainmentConfiguration,
    GeofenceClient,
    Geolocation,
    GroupedLight,
    GroupedLightLevel,
    GroupedMotion,
    Homekit,
    Light,
    LightLevel,
    Matter,
    MatterFabric,
    Motion,
    RelativeRotary,
    Room,
    Scene,
    ServiceGroup,
    SmartScene,
    Tamper,
    Temperature,
    WifiConnectivity,
    ZgpConnectivity,
    ZigbeeConnectivity,
    ZigbeeDeviceDiscovery,
    Zone,
)

if TYPE_CHECKING:
    from huepy.client.protocol import HueClient, Transport


@final
class HueAPI:
    """The complete typed bridge-facing API, addressed exclusively by id."""

    def __init__(self, hue: HueClient) -> None:
        """Create every typed resource endpoint for ``hue``."""
        self._hue = hue
        self.lights = Light(hue)
        self.grouped_lights = GroupedLight(hue)
        self.light_levels = LightLevel(hue)
        self.grouped_light_levels = GroupedLightLevel(hue)
        self.rooms = Room(hue)
        self.zones = Zone(hue)
        self.scenes = Scene(hue)
        self.smart_scenes = SmartScene(hue)
        self.devices = Device(hue)
        self.device_powers = DevicePower(hue)
        self.bridges = Bridge(hue)
        self.bridge_homes = BridgeHome(hue)
        self.service_groups = ServiceGroup(hue)
        self.motions = Motion(hue)
        self.grouped_motions = GroupedMotion(hue)
        self.temperatures = Temperature(hue)
        self.buttons = Button(hue)
        self.contacts = Contact(hue)
        self.relative_rotaries = RelativeRotary(hue)
        self.zigbee_connectivities = ZigbeeConnectivity(hue)
        self.zgp_connectivities = ZgpConnectivity(hue)
        self.wifi_connectivities = WifiConnectivity(hue)
        self.entertainments = Entertainment(hue)
        self.entertainment_configurations = EntertainmentConfiguration(hue)
        self.behavior_scripts = BehaviorScript(hue)
        self.behavior_instances = BehaviorInstance(hue)
        self.geolocations = Geolocation(hue)
        self.geofence_clients = GeofenceClient(hue)
        self.zigbee_device_discoveries = ZigbeeDeviceDiscovery(hue)
        self.device_software_updates = DeviceSoftwareUpdate(hue)
        self.homekits = Homekit(hue)
        self.matters = Matter(hue)
        self.matter_fabrics = MatterFabric(hue)
        self.tampers = Tamper(hue)
        self.camera_motions = CameraMotion(hue)

    @property
    def raw(self) -> Transport:
        """The open decoded-JSON transport for advanced API operations."""
        return self._hue.http
