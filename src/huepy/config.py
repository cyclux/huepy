"""Connection settings for a Hue bridge.

The bridge address, the application key and the location of the config file
all resolve the same way: explicit argument, then environment variable, then a
sensible default. Persisting the first two means a configured machine needs no
arguments and no environment at all.

The file holds a credential granting full control of the bridge, so
:meth:`HueConfig.save` restricts it to its owner -- and warns if the
filesystem silently refuses, rather than leaving a false assurance in place.

Typical usage example:

    config = HueConfig(bridge_ip="192.168.1.100")
    config.save()  # later runs need no argument
"""

import json
import os
import stat
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

APP_NAME = "huepy"
CONFIG_FILENAME = "config.json"

ENV_BRIDGE_IP = "HUE_BRIDGE_IP"
ENV_APP_KEY = "HUE_APP_KEY"
ENV_CONFIG_PATH = "HUE_CONFIG_PATH"
ENV_XDG_CONFIG_HOME = "XDG_CONFIG_HOME"

KEY_BRIDGE_IP = "bridge_ip"
KEY_APP_KEY = "app_key"

CREDENTIAL_MODE = stat.S_IRUSR | stat.S_IWUSR
"""Owner read/write only: the stored key controls the whole bridge."""


class InsecureConfigWarning(UserWarning):
    """The config file's permissions could not be restricted to its owner.

    Raised on filesystems that ignore ``chmod`` -- a Windows drive mounted
    under WSL, for instance -- where the credential stays world-readable.
    """


def default_config_path() -> Path:
    """Where settings live when the caller names no path.

    Honours ``HUE_CONFIG_PATH``, then the XDG base directory spec, so the file
    lands under the user's home rather than the current working directory. A
    relative default would tie the stored credential to wherever a program
    happened to be started from.

    Returns:
        The resolved path to the config file.

    """
    override = os.getenv(ENV_CONFIG_PATH)
    if override:
        return Path(override).expanduser()

    xdg = os.getenv(ENV_XDG_CONFIG_HOME)
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / APP_NAME / CONFIG_FILENAME


@dataclass
class HueConfig:
    """Resolved configuration for a bridge connection.

    Attributes:
        bridge_ip: Address of the bridge on the local network.
        app_key: The Hue application key, if one is known yet.
        config_path: Where the settings are persisted.
        verify_ssl: Whether to verify the bridge's TLS certificate. Bridges
            ship a self-signed certificate, so this is off by default.

    """

    bridge_ip: str = ""
    app_key: str | None = None
    config_path: Path = field(default_factory=default_config_path)
    verify_ssl: bool = False

    def __post_init__(self) -> None:
        """Resolve settings from the environment and the config file.

        Raises:
            ValueError: If no bridge address is available from any source.

        """
        self.config_path = Path(self.config_path).expanduser()
        stored = self._read_stored()

        self.bridge_ip = (
            self.bridge_ip
            or os.getenv(ENV_BRIDGE_IP, "")
            or _as_str(stored.get(KEY_BRIDGE_IP))
            or ""
        )
        if not self.bridge_ip:
            msg = (
                f"No Hue bridge address configured. Pass bridge_ip=..., set "
                f"{ENV_BRIDGE_IP}, or store one with HueConfig.save(). "
                f"Looked in: {self.config_path}"
            )
            raise ValueError(msg)

        self.app_key = (
            self.app_key or os.getenv(ENV_APP_KEY) or _as_str(stored.get(KEY_APP_KEY))
        )

    def _read_stored(self) -> dict[str, object]:
        """Read the config file, or an empty mapping if absent or invalid."""
        if not self.config_path.is_file():
            return {}
        try:
            stored: object = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(stored, dict):
            return {}
        return cast("dict[str, object]", stored)

    def save(self, app_key: str | None = None) -> None:
        """Persist the bridge address and application key.

        The file is restricted to its owner; if the filesystem ignores that,
        an :class:`InsecureConfigWarning` is issued rather than leaving the
        caller believing the credential is protected.

        Args:
            app_key: A key to store, replacing any previously stored one. When
                omitted the current key is kept, so calling ``save()`` with no
                argument persists the bridge address on its own.

        """
        if app_key is not None:
            self.app_key = app_key

        contents: dict[str, str] = {KEY_BRIDGE_IP: self.bridge_ip}
        if self.app_key:
            contents[KEY_APP_KEY] = self.app_key

        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        _ = self.config_path.write_text(json.dumps(contents), encoding="utf-8")
        self._restrict_permissions()

    def _restrict_permissions(self) -> None:
        """Restrict the config file to its owner, warning if that fails."""
        try:
            self.config_path.chmod(CREDENTIAL_MODE)
            actual = stat.S_IMODE(self.config_path.stat().st_mode)
        except OSError as exc:
            message = (
                f"Could not restrict permissions on {self.config_path}: {exc}. "
                f"The Hue application key it holds may be readable by others."
            )
            warnings.warn(message, InsecureConfigWarning, stacklevel=3)
            return

        if actual != CREDENTIAL_MODE:
            message = (
                f"{self.config_path} is mode {actual:o}, not {CREDENTIAL_MODE:o}: "
                f"this filesystem ignores chmod, so the Hue application key it "
                f"holds is readable by others. Store it on a filesystem that "
                f"supports POSIX permissions, e.g. via {ENV_CONFIG_PATH}."
            )
            warnings.warn(message, InsecureConfigWarning, stacklevel=3)


def _as_str(value: object) -> str | None:
    """Return ``value`` when it is a string, otherwise None."""
    return value if isinstance(value, str) else None
