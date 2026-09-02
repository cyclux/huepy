"""Reading plan files, and merging a directory of them.

The merge rules are what let a flat be split one file per room, so each of
them gets a test: what may be repeated, what may not, and which file gets
the blame.
"""

import pytest

from huepy.exceptions import PlanError
from huepy.plans.loader import load_plan, load_plans

ROOM = """
version = 1
[[scenario]]
name = "{name}"
scope = ["room:{name}"]
[[scenario.step]]
at = "09:00"
set = {{ brightness = 100 }}
"""

LOCATION = """
[location]
latitude = 48.137
longitude = 11.575
"""


def write(directory, filename, text):
    path = directory / filename
    path.write_text(text)
    return path


class TestOneFile:
    def test_a_file_loads(self, tmp_path):
        path = write(tmp_path, "a.toml", ROOM.format(name="A"))
        assert load_plan(path).scenario[0].name == "A"

    def test_a_missing_file_names_itself(self, tmp_path):
        with pytest.raises(PlanError, match=r"missing\.toml: no such plan file"):
            _ = load_plan(tmp_path / "missing.toml")

    def test_invalid_toml_names_the_file(self, tmp_path):
        path = write(tmp_path, "bad.toml", "version = 1\nthis is not toml")
        with pytest.raises(PlanError, match=r"bad\.toml: is not valid TOML"):
            _ = load_plan(path)

    def test_an_invalid_plan_names_the_key_path(self, tmp_path):
        path = write(
            tmp_path,
            "bad.toml",
            """
version = 1
[[scenario]]
name = "x"
scope = ["room:X"]
[[scenario.step]]
at = "09:00"
set = { brightnes = 100 }
""",
        )
        with pytest.raises(PlanError, match=r"scenario\.0\.step\.0\.set\.brightnes"):
            _ = load_plan(path)

    def test_load_plans_accepts_a_single_file_too(self, tmp_path):
        path = write(tmp_path, "a.toml", ROOM.format(name="A"))
        assert len(load_plans(path).scenario) == 1


class TestDirectory:
    def test_files_merge_in_filename_order(self, tmp_path):
        _ = write(tmp_path, "b.toml", ROOM.format(name="B"))
        _ = write(tmp_path, "a.toml", ROOM.format(name="A"))
        assert [s.name for s in load_plans(tmp_path).scenario] == ["A", "B"]

    def test_only_toml_files_are_read(self, tmp_path):
        _ = write(tmp_path, "a.toml", ROOM.format(name="A"))
        _ = write(tmp_path, "notes.txt", "not a plan")
        assert len(load_plans(tmp_path).scenario) == 1

    def test_an_empty_directory_is_an_error(self, tmp_path):
        with pytest.raises(PlanError, match=r"contains no \.toml plan files"):
            _ = load_plans(tmp_path)

    def test_a_missing_path_is_an_error(self, tmp_path):
        with pytest.raises(PlanError, match="no such plan file or directory"):
            _ = load_plans(tmp_path / "nowhere")

    def test_location_may_live_in_its_own_file(self, tmp_path):
        _ = write(tmp_path, "00-flat.toml", LOCATION)
        _ = write(tmp_path, "living.toml", ROOM.format(name="Living"))
        plan = load_plans(tmp_path)
        assert plan.location is not None
        assert plan.location.latitude == pytest.approx(48.137)

    def test_location_declared_twice_blames_the_second_file(self, tmp_path):
        _ = write(tmp_path, "a.toml", LOCATION)
        _ = write(tmp_path, "b.toml", LOCATION)
        with pytest.raises(
            PlanError, match=r"b\.toml: declares \[location\], but a\.toml"
        ):
            _ = load_plans(tmp_path)

    def test_defaults_declared_twice_is_an_error(self, tmp_path):
        _ = write(tmp_path, "a.toml", "[defaults]\nramp = '1s'\n")
        _ = write(tmp_path, "b.toml", "[defaults]\nramp = '2s'\n")
        with pytest.raises(PlanError, match=r"declares \[defaults\]"):
            _ = load_plans(tmp_path)

    def test_versions_must_agree(self, tmp_path):
        _ = write(tmp_path, "a.toml", ROOM.format(name="A"))
        _ = write(
            tmp_path,
            "b.toml",
            ROOM.format(name="B").replace("version = 1", "version = 2"),
        )
        with pytest.raises(
            PlanError, match=r"b\.toml: declares version 2, but a\.toml"
        ):
            _ = load_plans(tmp_path)

    def test_a_missing_version_defaults_to_one(self, tmp_path):
        _ = write(
            tmp_path, "a.toml", ROOM.format(name="A").replace("version = 1\n", "")
        )
        assert load_plans(tmp_path).version == 1

    def test_an_unknown_top_level_key_names_the_file(self, tmp_path):
        # At the top: after a table header the key would belong to the table.
        _ = write(tmp_path, "a.toml", "scenarios = 3\n" + ROOM.format(name="A"))
        with pytest.raises(
            PlanError, match=r"a\.toml: has unknown top-level keys: scenarios"
        ):
            _ = load_plans(tmp_path)

    def test_scenario_as_a_table_is_an_error(self, tmp_path):
        # `[scenario]` instead of `[[scenario]]` is the classic TOML slip.
        _ = write(tmp_path, "a.toml", "[scenario]\nname = 'x'\n")
        with pytest.raises(PlanError, match=r"a\.toml: 'scenario' must be a list"):
            _ = load_plans(tmp_path)

    def test_a_scenario_name_repeated_across_files_names_both(self, tmp_path):
        _ = write(tmp_path, "a.toml", ROOM.format(name="Same"))
        _ = write(tmp_path, "b.toml", ROOM.format(name="Same"))
        with pytest.raises(
            PlanError, match=r"b\.toml: declares scenario 'Same', but a\.toml"
        ):
            _ = load_plans(tmp_path)

    def test_a_scenario_name_repeated_in_one_file_names_it(self, tmp_path):
        _ = write(
            tmp_path,
            "a.toml",
            ROOM.format(name="Same")
            + ROOM.format(name="Same").replace("version = 1\n", ""),
        )
        with pytest.raises(PlanError, match=r"a\.toml: declares scenario 'Same' twice"):
            _ = load_plans(tmp_path)
