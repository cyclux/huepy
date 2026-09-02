"""The command line interface.

Three of the five verbs must never touch a bridge -- that is the whole point of
being able to check a plan before running it -- so those tests pass no client
at all and would fail loudly if one were needed. ``validate`` is the exception,
and gets the shared transport fake.
"""

import asyncio
import json
import logging
import signal
import sys

import pytest

from huepy.cli import (
    EXIT_FAILED,
    EXIT_OK,
    _binding_report,
    _listen_address,
    _log_level,
    _stopping_on_signals,
    main,
)
from huepy.exceptions import PlanError
from huepy.models import parse_resource
from huepy.plans.resolve import bind
from huepy.plans.schema import Plan
from huepy.plans.signals import SignalServer

from .conftest import envelope

PLAN = """
version = 1

[location]
latitude = 48.137
longitude = 11.575
timezone = "Europe/Berlin"

[[scenario]]
name = "lr"
scope = ["room:Living Room"]

[[scenario.step]]
at = "09:00"
ramp = "1h"
set = { brightness = 100, kelvin = 5000 }

[[scenario.step]]
at = "sunset+30m"
ramp = "3h"
set = { brightness = 20, kelvin = 2200 }
"""


@pytest.fixture
def plan_file(tmp_path):
    path = tmp_path / "flat.toml"
    path.write_text(PLAN)
    return str(path)


class TestCheck:
    def test_a_good_plan_passes(self, plan_file, capsys):
        assert main(["plan", "check", plan_file]) == EXIT_OK
        assert "1 scenarios" in capsys.readouterr().out

    def test_a_malformed_file_fails_with_the_filename(self, tmp_path, capsys):
        bad = tmp_path / "bad.toml"
        bad.write_text("version = 1\nthis is not toml")
        assert main(["plan", "check", str(bad)]) == EXIT_FAILED
        assert "bad.toml" in capsys.readouterr().err

    def test_an_unknown_timezone_fails_at_check(self, tmp_path, capsys):
        bad = tmp_path / "tz.toml"
        bad.write_text(
            """
version = 1
[location]
latitude = 48.1
longitude = 11.5
timezone = "Mars/Olympus"
"""
        )
        assert main(["plan", "check", str(bad)]) == EXIT_FAILED
        assert "timezone" in capsys.readouterr().err

    def test_an_invalid_plan_names_the_key(self, tmp_path, capsys):
        bad = tmp_path / "bad.toml"
        bad.write_text(
            """
version = 1
[[scenario]]
name = "x"
scope = ["room:X"]
[[scenario.step]]
at = "moonrise"
set = { brightness = 5 }
"""
        )
        assert main(["plan", "check", str(bad)]) == EXIT_FAILED
        assert "moonrise" in capsys.readouterr().err

    def test_a_missing_file_fails(self, tmp_path, capsys):
        assert main(["plan", "check", str(tmp_path / "nope.toml")]) == EXIT_FAILED
        assert "no such plan file" in capsys.readouterr().err


class TestExplain:
    def test_it_resolves_solar_anchors_to_real_times(self, plan_file, capsys):
        argv = ["plan", "explain", plan_file, "--at", "2026-09-01T12:00"]
        assert main(argv) == EXIT_OK
        out = capsys.readouterr().out
        assert "09:00:00" in out
        # Sunset over Munich on 1 September, plus thirty minutes.
        assert "20:25" in out

    def test_it_reports_how_many_requests_a_step_costs(self, plan_file, capsys):
        # The three-hour ramp outruns the bridge's single-PUT ceiling, so it
        # must be shown as two requests rather than one.
        main(["plan", "explain", plan_file, "--at", "2026-09-01T12:00"])
        out = capsys.readouterr().out
        assert "(1 request)" in out
        assert "(2 requests)" in out

    def test_it_honours_weekday_recurrence(self, tmp_path, capsys):
        path = tmp_path / "w.toml"
        path.write_text(
            """
version = 1
[[scenario]]
name = "weekend"
scope = ["room:X"]
days = ["saturday"]
[[scenario.step]]
at = "10:30"
set = { brightness = 100 }
"""
        )
        main(["plan", "explain", str(path), "--at", "2026-09-01T12:00"])
        assert "no steps today" in capsys.readouterr().out

    def test_a_naive_at_is_read_in_the_plans_zone(self, tmp_path, capsys):
        # A bare clock time on the command line means the plan's clock, not
        # the host's. With the host east of the plan, the host's reading of
        # 00:30 would describe the previous day.
        path = tmp_path / "la.toml"
        path.write_text(
            """
version = 1
[location]
latitude = 34.05
longitude = -118.24
timezone = "America/Los_Angeles"
[[scenario]]
name = "x"
scope = ["room:X"]
[[scenario.step]]
at = "09:00"
set = { brightness = 100 }
"""
        )
        main(["plan", "explain", str(path), "--at", "2026-09-01T00:30"])
        assert "Plan for 2026-09-01 (America/Los_Angeles)" in capsys.readouterr().out

    def test_a_plan_without_a_zone_says_so(self, tmp_path, capsys):
        path = tmp_path / "bare.toml"
        path.write_text(
            """
version = 1
[[scenario]]
name = "x"
scope = ["room:X"]
[[scenario.step]]
at = "09:00"
set = { brightness = 100 }
"""
        )
        main(["plan", "explain", str(path), "--at", "2026-09-01T12:00"])
        assert "(host zone)" in capsys.readouterr().out

    def test_a_long_first_step_is_costed_from_yesterday(self, tmp_path, capsys):
        # The first step of the day fades from yesterday's last, so its
        # request count must be the chained count the runner would send.
        path = tmp_path / "first.toml"
        path.write_text(
            """
version = 1
[[scenario]]
name = "x"
scope = ["room:X"]
[[scenario.step]]
at = "06:00"
ramp = "3h"
set = { brightness = 100 }
[[scenario.step]]
at = "22:00"
set = { brightness = 10 }
"""
        )
        main(["plan", "explain", str(path), "--at", "2026-09-01T12:00"])
        assert "(2 requests)" in capsys.readouterr().out


LEVEL_PLAN = """
version = 1

[[scenario]]
name = "dusk"
scope = ["room:Living Room"]

[[scenario.rule]]
when = "light_level:Window sensor"
below = 30
hold = "90s"
set = { on = true, brightness = 15 }
"""


class TestExplainLevels:
    def test_a_level_rule_prints_its_threshold_and_release(self, tmp_path, capsys):
        path = tmp_path / "dusk.toml"
        _ = path.write_text(LEVEL_PLAN)
        assert (
            main(["plan", "explain", str(path), "--at", "2026-09-01T12:00"]) == EXIT_OK
        )
        out = capsys.readouterr().out
        assert (
            "on light_level:Window sensor below 30 lux: on=True brightness=15, "
            "hold 1m30s after it brightens past 150 lux"
        ) in out


class TestHelp:
    def test_every_verb_is_listed(self, capsys):
        with pytest.raises(SystemExit) as stopped:
            _ = main(["plan", "--help"])
        assert stopped.value.code == EXIT_OK
        out = capsys.readouterr().out
        for verb in ("check", "explain", "validate", "run", "signal", "schema"):
            assert verb in out


def report_resources():
    return [
        {
            "id": "room-living",
            "type": "room",
            "metadata": {"name": "Living Room"},
            "children": [{"rid": "dev-lamp", "rtype": "device"}],
            "services": [{"rid": "gl-living", "rtype": "grouped_light"}],
        },
        {
            "id": "light-1",
            "type": "light",
            "metadata": {"name": "Corner Lamp"},
            "owner": {"rid": "dev-lamp", "rtype": "device"},
        },
        {
            "id": "dev-hall",
            "type": "device",
            "metadata": {"name": "Hall sensor"},
            "services": [{"rid": "motion-1", "rtype": "motion"}],
        },
        {
            "id": "dev-dimmer",
            "type": "device",
            "metadata": {"name": "Dimmer"},
            "services": [
                {"rid": "button-1", "rtype": "button"},
                {"rid": "button-2", "rtype": "button"},
            ],
        },
        {
            "id": "motion-1",
            "type": "motion",
            "enabled": False,
            "owner": {"rid": "dev-hall", "rtype": "device"},
        },
    ]


REPORT_PLAN = {
    "version": 1,
    "scenario": [
        {
            "name": "lr",
            "scope": ["room:Living Room"],
            "step": [{"at": "09:00", "set": {"brightness": 100}}],
            "rule": [
                {"when": "motion:Hall sensor", "set": {"on": True}},
                {"when": "button:Dimmer", "set": {"on": False}},
            ],
        },
        {
            "name": "movie",
            "scope": ["light:Corner Lamp"],
            "activate_on": "signal:movie_started",
            "set": {"brightness": 8},
        },
    ],
}


def squeezed(lines):
    return [" ".join(line.split()) for line in lines]


class TestValidateReport:
    def report(self):
        resources = [parse_resource(r) for r in report_resources()]
        plan = Plan.model_validate(REPORT_PLAN)
        return squeezed(_binding_report(plan, bind(resources, plan), resources))

    def test_the_summary_comes_first(self):
        assert self.report()[0] == "OK: 2 scenarios, 2 scopes, 3 triggers all resolve"

    def test_a_scope_line_names_the_path_and_the_lights_behind_it(self):
        lines = self.report()
        assert (
            "lr room:Living Room -> grouped_light/gl-living (1 light: Corner Lamp)"
            in lines
        )
        assert (
            "movie light:Corner Lamp -> light/light-1 (1 light: Corner Lamp)" in lines
        )

    def test_trigger_lines_show_services_and_signals(self):
        lines = self.report()
        assert "motion:Hall sensor -> motion motion-1" in lines
        assert "button:Dimmer -> 2 button services: button-1, button-2" in lines
        assert "signal:movie_started -> application signal" in lines

    def test_a_disabled_sensor_is_listed_under_warnings(self):
        lines = self.report()
        assert lines.index("warnings") > lines.index("triggers")
        assert (
            "motion:Hall sensor: the sensor is disabled on the bridge, "
            "so this trigger will never fire"
        ) in lines


class Ready:
    """A stand-in for ``Hue(...)`` that yields an already-wired client."""

    def __init__(self, client):
        self.client = client

    async def __aenter__(self):
        return self.client

    async def __aexit__(self, *_):
        return None


class TestValidate:
    def test_it_prints_the_report(self, hue, http, plan_file, monkeypatch, capsys):
        http.queue("/clip/v2/resource", envelope(*report_resources()))
        monkeypatch.setattr("huepy.cli.Hue", lambda *_args, **_kwargs: Ready(hue))
        assert main(["plan", "validate", str(plan_file)]) == EXIT_OK
        out = squeezed(capsys.readouterr().out.splitlines())
        assert out[0] == "OK: 1 scenarios, 1 scopes, 0 triggers all resolve"
        assert (
            "lr room:Living Room -> grouped_light/gl-living (1 light: Corner Lamp)"
            in out
        )

    def test_an_unknown_name_fails_with_what_exists(
        self, hue, http, tmp_path, monkeypatch, capsys
    ):
        http.queue("/clip/v2/resource", envelope(*report_resources()))
        monkeypatch.setattr("huepy.cli.Hue", lambda *_args, **_kwargs: Ready(hue))
        path = tmp_path / "typo.toml"
        path.write_text(PLAN.replace("room:Living Room", "room:Livng Room"))
        assert main(["plan", "validate", str(path)]) == EXIT_FAILED
        err = capsys.readouterr().err
        assert "room:Livng Room: no such room. Known: Living Room" in err


GUARD = "s3cret"


class TestSignal:
    async def test_it_fires_and_prints_the_outcomes(self, capsys):
        fired: list[str] = []

        def fire(name: str) -> tuple[str, ...]:
            fired.append(name)
            return (f"activated {name!r}",)

        async with SignalServer(fire, {"movie_started"}, port=0) as server:
            url = f"http://127.0.0.1:{server.port}"
            # main() blocks on the request, and the server answering it lives
            # on this loop, so the call has to leave the loop free.
            code = await asyncio.to_thread(
                main, ["plan", "signal", "movie_started", "--url", url]
            )
        assert code == EXIT_OK
        assert fired == ["movie_started"]
        assert "activated 'movie_started'" in capsys.readouterr().out

    async def test_an_unknown_signal_fails_and_lists_the_known_ones(self, capsys):
        async with SignalServer(lambda _name: (), {"movie_started"}, port=0) as server:
            url = f"http://127.0.0.1:{server.port}"
            code = await asyncio.to_thread(
                main, ["plan", "signal", "doorbell", "--url", url]
            )
        assert code == EXIT_FAILED
        err = capsys.readouterr().err
        assert "no signal 'doorbell'" in err
        assert "listens for: movie_started" in err

    async def test_a_guarded_plan_wants_the_token(self, capsys):
        async with SignalServer(lambda _n: (), {"x"}, port=0, token=GUARD) as server:
            url = f"http://127.0.0.1:{server.port}"
            refused = await asyncio.to_thread(
                main, ["plan", "signal", "x", "--url", url]
            )
            accepted = await asyncio.to_thread(
                main, ["plan", "signal", "x", "--url", url, "--token", GUARD]
            )
        assert refused == EXIT_FAILED
        assert accepted == EXIT_OK
        assert "requires a token" in capsys.readouterr().err

    def test_nothing_listening_fails_with_a_hint(self, capsys):
        code = main(["plan", "signal", "x", "--url", "http://127.0.0.1:1"])
        assert code == EXIT_FAILED
        assert "is 'huepy plan run' running?" in capsys.readouterr().err


class TestListen:
    def test_host_and_port(self):
        assert _listen_address("0.0.0.0:9000") == ("0.0.0.0", 9000)  # noqa: S104 - a parsing test

    def test_a_bare_port_uses_the_default_host(self):
        assert _listen_address("9000") == ("127.0.0.1", 9000)

    def test_a_bad_port_is_an_error(self):
        with pytest.raises(PlanError, match="HOST:PORT"):
            _ = _listen_address("localhost:http")


class TestVerbosity:
    @pytest.mark.parametrize(
        ("verbose", "quiet", "level"),
        [
            (0, False, logging.WARNING),
            (1, False, logging.INFO),
            (2, False, logging.DEBUG),
            (3, False, logging.DEBUG),
            (0, True, logging.ERROR),
            (2, True, logging.ERROR),
        ],
    )
    def test_flags_map_to_levels(self, verbose, quiet, level):
        assert _log_level(verbose, quiet=quiet) == level


@pytest.mark.skipif(sys.platform == "win32", reason="no loop signal handlers")
class TestStopSignals:
    async def test_sigterm_calls_stop(self):
        stopped = asyncio.Event()
        with _stopping_on_signals(stopped.set):
            signal.raise_signal(signal.SIGTERM)
            await asyncio.wait_for(stopped.wait(), timeout=1.0)

    async def test_a_second_signal_cancels_the_task(self):
        # `stop` only takes effect between writes; a bridge that has stopped
        # answering holds a write for minutes, and the second signal must
        # not wait for it.
        asked: list[int] = []

        async def body() -> None:
            with _stopping_on_signals(lambda: asked.append(1)):
                signal.raise_signal(signal.SIGTERM)
                await asyncio.sleep(0.05)
                signal.raise_signal(signal.SIGTERM)
                await asyncio.sleep(1)

        task = asyncio.create_task(body())
        with pytest.raises(asyncio.CancelledError):
            await task
        assert asked == [1]

    async def test_handlers_are_removed_afterwards(self):
        with _stopping_on_signals(lambda: None):
            assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


class TestSchema:
    def test_it_emits_valid_json_schema(self, capsys):
        assert main(["plan", "schema"]) == EXIT_OK
        schema = json.loads(capsys.readouterr().out)
        assert "Scenario" in schema["$defs"]

    def test_the_schema_forbids_unknown_keys(self, capsys):
        # This is what gives an editor the red squiggle on a typo.
        main(["plan", "schema"])
        schema = json.loads(capsys.readouterr().out)
        assert schema["$defs"]["Action"]["additionalProperties"] is False
