"""Guards against API_REFERENCE.md drifting from the code.

The previous reference documented `hue.api.service_groups`, which did not exist for
about a year. These tests make that class of drift fail the build: every
handler, model command, public name and capability claim in the reference is
checked against the objects it describes.
"""

import inspect
import re
import textwrap
from pathlib import Path

import pytest

import huepy
from huepy import Hue, color, models
from huepy.resources.base import NamedResourceHandler

REFERENCE = Path(__file__).resolve().parent.parent / "API_REFERENCE.md"

# The names samples in the reference give to bound models, and the model each
# one stands for. A sample that introduces a new receiver has to be registered
# here, which is what keeps `test_documented_calls_exist` honest.
SAMPLE_RECEIVERS: dict[str, type[models.HueModel]] = {
    "desk": models.Light,
    "detached": models.Light,
    "downstairs": models.Zone,
    "kitchen": models.Room,
    "light": models.Light,
    "resource": models.HueResource,
    "room": models.Room,
    "scene": models.Scene,
    "strip": models.Light,
}

# `await x.y(...)` and `async for e in x.y(...)`: both issue a call the
# reference is promising exists.
AWAITED_CALL = re.compile(r"(?:await|async for \w+ in) ([\w.]+)\.(\w+)\(")

PYTHON_BLOCK = re.compile(r"^```python\n(.*?)^```", re.DOTALL | re.MULTILINE)


@pytest.fixture(scope="module")
def reference_text() -> str:
    return REFERENCE.read_text(encoding="utf-8")


def handler_names(client: Hue) -> set[str]:
    return {
        name for name, value in vars(client.api).items() if hasattr(value, "base_url")
    }


def resolve(root: object, path: str) -> object | None:
    """Walk a dotted attribute path, e.g. ``hue.http``, from ``root``."""
    current = root
    for part in path.split(".")[1:]:
        current = getattr(current, part, None)
        if current is None:
            return None
    return current


def test_reference_exists():
    assert REFERENCE.is_file()


def test_every_documented_client_attribute_exists(reference_text, hue):
    documented = set(re.findall(r"`hue\.(\w+)`", reference_text))
    missing = {name for name in documented if not hasattr(hue, name)}
    assert not missing, f"documented but absent from Hue: {sorted(missing)}"


def test_every_handler_is_documented(reference_text, hue):
    undocumented = {
        name for name in handler_names(hue) if f"`hue.api.{name}`" not in reference_text
    }
    assert not undocumented, (
        f"handlers missing from the reference: {sorted(undocumented)}"
    )


def test_documented_handler_methods_exist(reference_text, hue):
    rows = re.findall(
        r"\| `hue\.api\.(\w+)` \| `\w+` \| `[\w.]+` \| (.+?) \|$",
        reference_text,
        re.MULTILINE,
    )
    assert rows, "the handler table could not be parsed"

    missing: list[str] = []
    for attribute, methods in rows:
        handler = getattr(hue.api, attribute, None)
        if handler is None:
            missing.append(f"hue.{attribute}")
            continue
        missing.extend(
            f"hue.api.{attribute}.{method}"
            for method in re.findall(r"`(\w+)`", methods)
            if not hasattr(handler, method)
        )
    assert not missing, f"documented but absent: {missing}"


def test_python_samples_are_valid_python(reference_text):
    """Catches samples that stop being runnable Python as the API moves.

    Every ``python`` block is compiled as an async function body, so top-level
    ``await`` is allowed. Signature listings belong in unfenced blocks, which
    are deliberately not compiled.
    """
    blocks = PYTHON_BLOCK.findall(reference_text)
    assert blocks, "no python samples found in the reference"

    broken: list[str] = []
    for index, source in enumerate(blocks, start=1):
        body = textwrap.indent(source, "    ")
        try:
            compile(f"async def _sample():\n{body}\n", f"<sample {index}>", "exec")
        except SyntaxError as exc:
            broken.append(f"sample {index}: {exc}")
    assert not broken, f"samples are not valid Python: {broken}"


def test_documented_calls_exist(reference_text, hue):
    """Catches a command being renamed, moved between models, or invented.

    Every ``await x.y(...)`` in the reference -- in prose, in a table or in a
    sample -- has to resolve: through the client for a ``hue.`` chain, and
    through :data:`SAMPLE_RECEIVERS` for a bound model.
    """
    problems: list[str] = []
    for receiver, method in set(AWAITED_CALL.findall(reference_text)):
        if receiver == "hue" or receiver.startswith("hue."):
            target = hue if receiver == "hue" else resolve(hue, receiver)
            if target is None:
                problems.append(f"{receiver} does not resolve on the client")
            elif not hasattr(target, method):
                problems.append(f"{receiver}.{method} does not exist")
        elif receiver in SAMPLE_RECEIVERS:
            model = SAMPLE_RECEIVERS[receiver]
            if not hasattr(model, method):
                problems.append(f"{model.__name__}.{method} does not exist")
        else:
            call = f"await {receiver}.{method}(...)"
            problems.append(f"{call}: add {receiver!r} to SAMPLE_RECEIVERS")
    assert not problems, f"documented calls that do not exist: {sorted(problems)}"


def test_every_public_name_is_documented(reference_text):
    """Catches a new export shipping without a line in the reference.

    ``huepy.__all__`` and ``huepy.color.__all__`` are the promises the package
    makes; an undocumented one is a promise nobody can find.
    """
    missing = [
        name
        for name in [*huepy.__all__, *color.__all__]
        if re.search(rf"\b{re.escape(name)}\b", reference_text) is None
    ]
    assert not missing, f"public names missing from the reference: {missing}"


def test_documented_model_names_exist(reference_text):
    """Catches a model that was renamed or dropped but still documented."""
    documented = set(re.findall(r"`models\.(\w+)", reference_text))
    missing = sorted(name for name in documented if not hasattr(models, name))
    assert not missing, f"documented but absent from huepy.models: {missing}"


def test_name_lookup_exists_only_on_high_level_collections(hue):
    """The typed API stays id-only while top-level collections resolve names."""
    names = ("lights", "rooms", "zones", "scenes", "devices", "service_groups")
    for name in names:
        collection = getattr(hue, name)
        assert all(hasattr(collection, method) for method in ("get", "list", "names"))
        handler = getattr(hue.api, name)
        assert isinstance(handler, NamedResourceHandler)
        assert not hasattr(handler, "by_name")
        assert not hasattr(handler, "names")


def test_removed_synonyms_are_absent(hue):
    """The breaking redesign ships one canonical spelling per operation."""
    for singular in ("light", "room", "zone", "scene", "device", "service_group"):
        assert not hasattr(hue, singular)
    for collection in (hue.lights, hue.rooms, hue.zones, hue.scenes, hue.devices):
        assert not hasattr(collection, "by_name")
        assert not hasattr(collection, "all")
        assert not hasattr(collection, "get_all")
        assert not hasattr(collection, "__getitem__")
    # `hue.state()` the factory and `hue.live_state` were folded into one
    # attribute. Pinning the descriptor is what stops the factory coming back:
    # a `state()` method would still satisfy every other assertion here.
    assert not hasattr(hue, "live_state")
    assert isinstance(inspect.getattr_static(Hue, "state"), property)
    assert not callable(hue.state)


def test_readme_does_not_advertise_the_removed_sync_api():
    readme = (REFERENCE.parent / "README.md").read_text(encoding="utf-8")
    for removed in ("nest_asyncio", "run_async", "is_jupyter", "filter_by"):
        assert removed not in readme, f"README still mentions removed {removed}"
