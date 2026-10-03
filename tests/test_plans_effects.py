"""Running a command for a rule's ``run`` effect."""

import sys

from huepy.plans.effects import flash_seconds, run_command


async def test_a_command_that_overstays_is_killed():
    result = await run_command(
        [sys.executable, "-c", "import time; time.sleep(30)"], kill_after=0.2
    )
    assert result.returncode is None
    assert not result.ok
    assert "killed after 0.2 s" in result.stderr


async def test_a_missing_program_is_reported_not_raised():
    result = await run_command(["/nonexistent/huepy-test"], kill_after=1)
    assert result.returncode is None
    assert "No such file" in result.stderr


async def test_success_is_exit_zero():
    result = await run_command([sys.executable, "-c", "pass"], kill_after=5)
    assert result.ok


def test_a_flash_lasts_a_second_per_blink():
    # Measured: a four-second on_off signal blinked four times.
    assert flash_seconds(4) == 4.0
