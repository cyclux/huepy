"""The command line interface.

Three of the five verbs must never touch a bridge -- that is the whole point of
being able to check a plan before running it -- so these tests pass no client at
all and would fail loudly if one were needed.
"""

import asyncio
import json
import logging
import signal
import sys

import pytest

from huepy.cli import EXIT_FAILED, EXIT_OK, _log_level, _stopping_on_signals, main

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


class TestHelp:
    def test_every_verb_is_listed(self, capsys):
        with pytest.raises(SystemExit) as stopped:
            _ = main(["plan", "--help"])
        assert stopped.value.code == EXIT_OK
        out = capsys.readouterr().out
        for verb in ("check", "explain", "validate", "run", "schema"):
            assert verb in out


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
