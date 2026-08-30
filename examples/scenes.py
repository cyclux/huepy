"""Recall a scene by name -- the most common thing you do with Hue.

    python examples/scenes.py "Relax"
    python examples/scenes.py "Relax" "My day"   # 2nd arg: a smart scene

A scene stores a lighting state for a room or zone; recalling it applies that
state to the whole group in one request. A smart scene is different: it runs a
scene schedule across the day, so it is started and stopped rather than
recalled once -- pass one as a second argument to see that.
"""

import asyncio
import sys

from huepy import Hue, ResourceNotFoundError
from huepy.models import RecallAction

FADE_SECONDS = 2.0
HOLD_SECONDS = 5.0


async def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        raise SystemExit(1)
    scene_name = args[0]

    async with Hue() as hue:
        try:
            scene = await hue.scenes.get(scene_name)
        except ResourceNotFoundError as exc:
            print(f"No scene named {exc.name!r}.")
            print(f"Known scenes: {', '.join(exc.known) or 'none'}")
            raise SystemExit(1) from None

        # The default recall applies the stored state once, fading over the
        # given time. `activate()` with no arguments sends exactly
        # {"recall": {"action": "active"}}.
        print(f"Recalling {scene.name!r} over {FADE_SECONDS:.0f}s...")
        await scene.activate(duration=FADE_SECONDS)
        await asyncio.sleep(HOLD_SECONDS)

        # The same scene can cycle its palette instead of applying once, if it
        # defines one -- what the Hue app calls a dynamic scene.
        print("Starting its dynamic palette...")
        await scene.activate(action=RecallAction.DYNAMIC_PALETTE)
        await asyncio.sleep(HOLD_SECONDS)

        # And by name, without fetching first: the collection resolves it and
        # can override the scene's own brightness on the way in.
        print(f"Recalling {scene_name!r} again, dimmed to 40%...")
        await hue.scenes.activate(scene_name, brightness=40.0)

        # A smart scene, if one was named, runs its daily schedule until stopped.
        for smart_name in args[1:2]:
            try:
                smart = await hue.smart_scenes.get(smart_name)
            except ResourceNotFoundError as exc:
                print(f"No smart scene named {exc.name!r}.")
                continue
            print(f"Starting smart scene {smart.name!r}, then stopping it...")
            await smart.activate()
            await asyncio.sleep(HOLD_SECONDS)
            await smart.deactivate()


if __name__ == "__main__":
    asyncio.run(main())
