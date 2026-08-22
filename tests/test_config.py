"""Tests for HueConfig resolution and credential persistence."""

import json
import stat
from pathlib import Path

import pytest

from huepy.config import (
    ENV_APP_KEY,
    ENV_BRIDGE_IP,
    ENV_CONFIG_PATH,
    ENV_XDG_CONFIG_HOME,
    HueConfig,
    InsecureConfigWarning,
    default_config_path,
)


@pytest.fixture(autouse=True)
def _clear_hue_env(monkeypatch):
    """Keep the developer's real environment out of these tests."""
    monkeypatch.delenv(ENV_BRIDGE_IP, raising=False)
    monkeypatch.delenv(ENV_APP_KEY, raising=False)
    monkeypatch.delenv(ENV_CONFIG_PATH, raising=False)
    monkeypatch.delenv(ENV_XDG_CONFIG_HOME, raising=False)


class TestBridgeIp:
    def test_explicit_value_is_used(self, tmp_path):
        config = HueConfig(bridge_ip="10.0.0.5", config_path=tmp_path / "c.json")
        assert config.bridge_ip == "10.0.0.5"

    def test_falls_back_to_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV_BRIDGE_IP, "10.0.0.9")
        config = HueConfig(config_path=tmp_path / "c.json")
        assert config.bridge_ip == "10.0.0.9"

    def test_falls_back_to_the_config_file(self, tmp_path):
        """A stored address means no argument and no environment are needed."""
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"bridge_ip": "10.0.0.7", "app_key": "k"}))
        assert HueConfig(config_path=path).bridge_ip == "10.0.0.7"

    def test_argument_wins_over_stored(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"bridge_ip": "10.0.0.7"}))
        assert HueConfig(bridge_ip="10.0.0.5", config_path=path).bridge_ip == "10.0.0.5"

    def test_environment_wins_over_stored(self, tmp_path, monkeypatch):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"bridge_ip": "10.0.0.7"}))
        monkeypatch.setenv(ENV_BRIDGE_IP, "10.0.0.9")
        assert HueConfig(config_path=path).bridge_ip == "10.0.0.9"

    def test_non_string_stored_value_is_ignored(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"bridge_ip": 1234}))
        with pytest.raises(ValueError, match="No Hue bridge address"):
            HueConfig(config_path=path)

    def test_missing_everywhere_raises(self, tmp_path):
        """No bridge address is an error, not a fallback to someone's home LAN."""
        with pytest.raises(ValueError, match="No Hue bridge address"):
            HueConfig(config_path=tmp_path / "c.json")

    def test_the_error_names_the_file_it_looked_in(self, tmp_path):
        path = tmp_path / "c.json"
        with pytest.raises(ValueError, match=str(path.name)):
            HueConfig(config_path=path)


class TestAppKey:
    def test_explicit_value_wins_over_file(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"app_key": "from-file"}))
        config = HueConfig(bridge_ip="10.0.0.5", app_key="explicit", config_path=path)
        assert config.app_key == "explicit"

    def test_environment_wins_over_file(self, tmp_path, monkeypatch):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"app_key": "from-file"}))
        monkeypatch.setenv(ENV_APP_KEY, "from-env")
        assert HueConfig(bridge_ip="10.0.0.5", config_path=path).app_key == "from-env"

    def test_read_from_config_file(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"app_key": "from-file"}))
        assert HueConfig(bridge_ip="10.0.0.5", config_path=path).app_key == "from-file"

    def test_absent_everywhere_is_none(self, tmp_path):
        assert (
            HueConfig(bridge_ip="10.0.0.5", config_path=tmp_path / "c.json").app_key
            is None
        )

    def test_malformed_config_file_is_ignored(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text("{not json")
        assert HueConfig(bridge_ip="10.0.0.5", config_path=path).app_key is None

    def test_config_file_without_app_key_is_ignored(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"other": 1}))
        assert HueConfig(bridge_ip="10.0.0.5", config_path=path).app_key is None

    def test_a_directory_at_the_config_path_is_ignored(self, tmp_path):
        path = tmp_path / "c.json"
        path.mkdir()
        assert HueConfig(bridge_ip="10.0.0.5", config_path=path).app_key is None


class TestSave:
    def test_save_to_bare_relative_filename(self, tmp_path, monkeypatch):
        """The default config_path is a bare filename with no directory part."""
        monkeypatch.chdir(tmp_path)
        config = HueConfig(bridge_ip="10.0.0.5", config_path=Path("config.json"))
        config.save("new-key")
        assert json.loads((tmp_path / "config.json").read_text()) == {
            "bridge_ip": "10.0.0.5",
            "app_key": "new-key",
        }

    def test_save_creates_missing_directories(self, tmp_path):
        path = tmp_path / "nested" / "deeper" / "config.json"
        HueConfig(bridge_ip="10.0.0.5", config_path=path).save("new-key")
        assert json.loads(path.read_text()) == {
            "bridge_ip": "10.0.0.5",
            "app_key": "new-key",
        }

    def test_save_updates_the_in_memory_key(self, tmp_path):
        config = HueConfig(bridge_ip="10.0.0.5", config_path=tmp_path / "c.json")
        config.save("new-key")
        assert config.app_key == "new-key"

    def test_saved_credential_is_owner_only(self, tmp_path):
        """The file holds a key granting full control of the bridge."""
        path = tmp_path / "c.json"
        HueConfig(bridge_ip="10.0.0.5", config_path=path).save("secret")
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {mode:o}"

    def test_save_overwrites_existing(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"app_key": "old"}))
        HueConfig(bridge_ip="10.0.0.5", config_path=path).save("new")
        assert json.loads(path.read_text()) == {
            "bridge_ip": "10.0.0.5",
            "app_key": "new",
        }


class TestPath:
    def test_string_paths_become_path_objects(self, tmp_path):
        """Strings are accepted at runtime even though the field is typed Path."""
        config = HueConfig(
            bridge_ip="10.0.0.5",
            config_path=str(tmp_path / "c.json"),  # pyright: ignore[reportArgumentType]
        )
        assert config.config_path.name == "c.json"

    def test_user_home_is_expanded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        config = HueConfig(bridge_ip="10.0.0.5", config_path=Path("~/c.json"))
        assert not str(config.config_path).startswith("~")


class TestRoundTrip:
    """What save() writes must be exactly what a later run reads back."""

    def test_saved_settings_need_no_argument_or_environment(self, tmp_path):
        path = tmp_path / "c.json"
        HueConfig(bridge_ip="10.0.0.5", config_path=path).save("stored-key")

        reloaded = HueConfig(config_path=path)
        assert reloaded.bridge_ip == "10.0.0.5"
        assert reloaded.app_key == "stored-key"

    def test_save_without_an_argument_persists_the_address_alone(self, tmp_path):
        path = tmp_path / "c.json"
        HueConfig(bridge_ip="10.0.0.5", config_path=path).save()

        assert json.loads(path.read_text()) == {"bridge_ip": "10.0.0.5"}
        assert HueConfig(config_path=path).bridge_ip == "10.0.0.5"

    def test_save_without_an_argument_keeps_an_existing_key(self, tmp_path):
        path = tmp_path / "c.json"
        config = HueConfig(bridge_ip="10.0.0.5", app_key="keep-me", config_path=path)
        config.save()

        assert HueConfig(config_path=path).app_key == "keep-me"

    def test_a_legacy_key_only_file_still_loads(self, tmp_path):
        """Files written before bridge_ip was persisted must keep working."""
        path = tmp_path / "c.json"
        path.write_text(json.dumps({"app_key": "old-style"}))

        config = HueConfig(bridge_ip="10.0.0.5", config_path=path)
        assert config.app_key == "old-style"

        config.save()
        assert HueConfig(config_path=path).bridge_ip == "10.0.0.5"


class TestDefaultLocation:
    """The default must not be a relative path.

    A credential stored at ./config.json belongs to whichever directory the
    program was started from -- which is how this repo ended up holding the
    same key twice, once at the root and once under examples/.
    """

    def test_default_is_absolute(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert default_config_path().is_absolute()

    def test_default_is_under_the_users_config_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert default_config_path() == tmp_path / ".config" / "huepy" / "config.json"

    def test_xdg_config_home_is_honoured(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV_XDG_CONFIG_HOME, str(tmp_path / "xdg"))
        assert default_config_path() == tmp_path / "xdg" / "huepy" / "config.json"

    def test_env_override_beats_xdg(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV_XDG_CONFIG_HOME, str(tmp_path / "xdg"))
        monkeypatch.setenv(ENV_CONFIG_PATH, str(tmp_path / "explicit.json"))
        assert default_config_path() == tmp_path / "explicit.json"

    def test_env_override_expands_the_home_shorthand(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(ENV_CONFIG_PATH, "~/somewhere.json")
        assert default_config_path() == tmp_path / "somewhere.json"

    def test_config_uses_the_default_when_no_path_is_given(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(ENV_BRIDGE_IP, "10.0.0.5")
        config = HueConfig()
        assert config.config_path == tmp_path / ".config" / "huepy" / "config.json"

    def test_an_explicit_path_still_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV_CONFIG_PATH, str(tmp_path / "from-env.json"))
        config = HueConfig(bridge_ip="10.0.0.5", config_path=tmp_path / "explicit.json")
        assert config.config_path == tmp_path / "explicit.json"

    def test_save_creates_the_config_directory(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv(ENV_BRIDGE_IP, "10.0.0.5")
        config = HueConfig()
        config.save("k")
        assert config.config_path.is_file()
        assert HueConfig().app_key == "k"


class TestPermissionWarning:
    """A filesystem that ignores chmod must not leave a false assurance."""

    def test_no_warning_when_permissions_stick(self, tmp_path, recwarn):
        HueConfig(bridge_ip="10.0.0.5", config_path=tmp_path / "c.json").save("k")
        assert not [w for w in recwarn if w.category is InsecureConfigWarning]

    def test_warns_when_the_mode_does_not_stick(self, tmp_path, monkeypatch):
        path = tmp_path / "c.json"
        config = HueConfig(bridge_ip="10.0.0.5", config_path=path)

        # Emulate a 9p/drvfs mount: chmod succeeds but the mode never changes.
        monkeypatch.setattr(Path, "chmod", lambda *_args, **_kwargs: None)

        with pytest.warns(InsecureConfigWarning, match="readable by others"):
            config.save("k")

    def test_warns_when_chmod_raises(self, tmp_path, monkeypatch):
        path = tmp_path / "c.json"
        config = HueConfig(bridge_ip="10.0.0.5", config_path=path)

        def refuse(*_args: object, **_kwargs: object) -> None:
            msg = "not supported"
            raise OSError(msg)

        monkeypatch.setattr(Path, "chmod", refuse)

        with pytest.warns(InsecureConfigWarning, match="Could not restrict"):
            config.save("k")

    def test_the_warning_names_the_override(self, tmp_path, monkeypatch):
        config = HueConfig(bridge_ip="10.0.0.5", config_path=tmp_path / "c.json")
        monkeypatch.setattr(Path, "chmod", lambda *_args, **_kwargs: None)
        with pytest.warns(InsecureConfigWarning, match=ENV_CONFIG_PATH):
            config.save("k")
