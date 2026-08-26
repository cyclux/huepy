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
from enum import StrEnum
from pathlib import Path
from typing import cast

APP_NAME = "huepy"
CONFIG_FILENAME = "config.json"

ENV_BRIDGE_IP = "HUE_BRIDGE_IP"
ENV_BRIDGE_ID = "HUE_BRIDGE_ID"
ENV_APP_KEY = "HUE_APP_KEY"
ENV_CONFIG_PATH = "HUE_CONFIG_PATH"
ENV_XDG_CONFIG_HOME = "XDG_CONFIG_HOME"
ENV_TLS = "HUE_TLS"
ENV_RATE_LIMIT = "HUE_RATE_LIMIT"

KEY_BRIDGE_IP = "bridge_ip"
KEY_BRIDGE_ID = "bridge_id"
KEY_APP_KEY = "app_key"

_FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})

CREDENTIAL_MODE = stat.S_IRUSR | stat.S_IWUSR
"""Owner read/write only: the stored key controls the whole bridge."""


class TlsMode(StrEnum):
    """How huepy validates the bridge's TLS certificate.

    Attributes:
        VERIFIED: Verify the certificate against Signify's bundled root CAs, and
            pin its common name to ``bridge_id`` when one is known. The default.
        INSECURE: Skip verification entirely -- development against a proxy or
            emulator only, never production.

    """

    VERIFIED = "verified"
    INSECURE = "insecure"


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
        tls: How to validate the bridge's TLS certificate. Genuine bridges carry
            a certificate signed by Signify's private CA, so verification is on
            by default; it degrades to certificate-only when ``bridge_id`` is
            unknown, and can be turned off with ``TlsMode.INSECURE``.
        bridge_id: The bridge id, used to pin the certificate's common name.
        rate_limit: Whether to pace writes to the bridge's throughput budget.

    """

    bridge_ip: str = ""
    app_key: str | None = None
    config_path: Path = field(default_factory=default_config_path)
    tls: TlsMode | None = None
    bridge_id: str | None = None
    rate_limit: bool | None = None

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
        self.bridge_id = (
            self.bridge_id
            or os.getenv(ENV_BRIDGE_ID)
            or _as_str(stored.get(KEY_BRIDGE_ID))
        )
        # A None field means the caller passed nothing, so the environment may
        # choose; an explicit argument is never None and always wins -- crucial
        # for tls, so a stale HUE_TLS cannot silently downgrade a caller who
        # explicitly asked to verify.
        self.tls = self._resolve_tls()
        self.rate_limit = self._resolve_rate_limit()

    def _resolve_tls(self) -> TlsMode:
        """Resolve the TLS mode: explicit argument, then env, then verified.

        Returns:
            The mode to use.

        Raises:
            ValueError: If ``HUE_TLS`` holds a value that is not a TLS mode.

        """
        if self.tls is not None:
            return self.tls
        env_tls = os.getenv(ENV_TLS)
        if not env_tls:
            return TlsMode.VERIFIED
        try:
            return TlsMode(env_tls.strip().lower())
        except ValueError:
            valid = ", ".join(repr(mode.value) for mode in TlsMode)
            msg = f"{ENV_TLS}={env_tls!r} is not a valid TLS mode; use {valid}"
            raise ValueError(msg) from None

    def _resolve_rate_limit(self) -> bool:
        """Resolve write pacing: explicit argument, then env, then on.

        Returns:
            Whether to pace writes.

        """
        if self.rate_limit is not None:
            return self.rate_limit
        env_rl = os.getenv(ENV_RATE_LIMIT)
        if env_rl is None:
            return True
        return env_rl.strip().lower() not in _FALSE_VALUES

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
        if self.bridge_id:
            contents[KEY_BRIDGE_ID] = self.bridge_id
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
