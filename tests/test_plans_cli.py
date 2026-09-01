"""The command line interface.

Three of the four verbs must never touch a bridge -- that is the whole point of
being able to check a plan before running it -- so these tests pass no client at
all and would fail loudly if one were needed.
"""

import json

import pytest

from huepy.cli import EXIT_FAILED, EXIT_OK, main

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
