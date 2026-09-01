"""Reading plan files off disk.

TOML, and only TOML. The format is hand-written, and YAML 1.1 -- which is what
PyYAML implements -- coerces the bare words ``on``, ``off``, ``yes`` and ``no``
into booleans. This format's most important key is ``on``, so ``set = { on =
false }`` would silently parse as ``{True: False}``. TOML also rejects a
duplicate key outright where YAML keeps the last one, so a copy-pasted ``at``
cannot quietly drop a step. ``tomllib`` is in the standard library, so none of
this costs a dependency.

A directory is a plan: every ``*.toml`` in it is loaded and merged, which is
what makes one file per room work. Only one file may declare ``[location]`` or
``[defaults]``, because two files disagreeing about where the flat is has no
sensible answer.

Typical usage example:

    plan = load_plans("./plans")
"""

import tomllib
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from huepy.exceptions import PlanError
from huepy.plans.schema import Plan

PLAN_SUFFIX = ".toml"
"""The only extension :func:`load_plans` picks up from a directory."""

_SINGLETON_SECTIONS = ("location", "defaults")


def _read_toml(path: Path) -> dict[str, Any]:
    """Parse one TOML file.

    Args:
        path: The file to read.

    Returns:
        The parsed document.

    Raises:
        PlanError: If the file is missing, unreadable, or not valid TOML.

    """
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as error:
        msg = "no such plan file"
        raise PlanError(msg, path) from error
    except OSError as error:
        msg = f"could not be read: {error.strerror}"
        raise PlanError(msg, path) from error
    except tomllib.TOMLDecodeError as error:
        msg = f"is not valid TOML: {error}"
        raise PlanError(msg, path) from error


def _describe(error: ValidationError, path: Path | None) -> PlanError:
    """Turn a pydantic error into one a plan author can act on.

    Pydantic reports locations as tuples of keys and indices. Rendered as a
    TOML-ish path they point at the line to fix.

    Args:
        error: The validation failure.
        path: The file being validated, when there is one.

    Returns:
        The error to raise.

    """
    lines: list[str] = []
    for detail in error.errors():
        where = ".".join(str(part) for part in detail["loc"]) or "<plan>"
        lines.append(f"  {where}: {detail['msg']}")
    body = "\n".join(lines)
    return PlanError(f"is not a valid plan:\n{body}", path)


def _validate(document: dict[str, Any], path: Path | None) -> Plan:
    """Validate a parsed document into a :class:`Plan`.

    Args:
        document: The parsed TOML.
        path: The file it came from, for error messages.

    Returns:
        The validated plan.

    Raises:
        PlanError: If the document is not a valid plan.

    """
    try:
        return Plan.model_validate(document)
    except ValidationError as error:
        raise _describe(error, path) from error


def load_plan(path: str | Path) -> Plan:
    """Load one plan file.

    Args:
        path: The ``.toml`` file to read.

    Returns:
        The validated plan.

    Raises:
        PlanError: If the file is missing, is not valid TOML, or is not a
            valid plan.

    """
    resolved = Path(path)
    return _validate(_read_toml(resolved), resolved)


def _merge(documents: list[tuple[Path, dict[str, Any]]]) -> dict[str, Any]:
    """Merge several plan documents into one.

    Scenario lists concatenate. ``[location]`` and ``[defaults]`` may be
    declared once across the whole set: two files disagreeing about where the
    flat is, or how long a default ramp lasts, has no defensible resolution,
    so it is an error rather than a last-one-wins race that depends on
    filename order.

    Args:
        documents: The parsed files, in load order.

    Returns:
        One merged document.

    Raises:
        PlanError: If two files declare the same singleton section, or
            disagree about the format version.

    """
    merged: dict[str, Any] = {"scenario": []}
    owners: dict[str, Path] = {}
    version_owner: Path | None = None

    for path, document in documents:
        for section in _SINGLETON_SECTIONS:
            if section not in document:
                continue
            if section in owners:
                msg = (
                    f"declares [{section}], but {owners[section].name} already "
                    f"does. Only one file in a plan directory may declare it"
                )
                raise PlanError(msg, path)
            owners[section] = path
            merged[section] = document[section]

        if "version" in document:
            if version_owner is not None and document["version"] != merged["version"]:
                msg = (
                    f"declares version {document['version']}, but "
                    f"{version_owner.name} declares {merged['version']}"
                )
                raise PlanError(msg, path)
            merged["version"] = document["version"]
            version_owner = path

        scenarios = document.get("scenario", [])
        if not isinstance(scenarios, list):
            msg = "'scenario' must be a list of [[scenario]] tables"
            raise PlanError(msg, path)
        merged["scenario"].extend(scenarios)

        unknown = set(document) - {"version", "scenario", *_SINGLETON_SECTIONS}
        if unknown:
            keys = ", ".join(sorted(unknown))
            msg = f"has unknown top-level keys: {keys}"
            raise PlanError(msg, path)

    merged.setdefault("version", 1)
    return merged


def load_plans(path: str | Path) -> Plan:
    """Load a plan from a file or a directory of files.

    Pointed at a directory, every ``*.toml`` inside it is loaded in filename
    order and merged, so a flat can be split one file per room.

    Args:
        path: A ``.toml`` file, or a directory containing some.

    Returns:
        The merged, validated plan.

    Raises:
        PlanError: If the path does not exist, a directory holds no plan
            files, any file is malformed, or the merged result is not a valid
            plan.

    """
    resolved = Path(path)
    if resolved.is_file():
        return load_plan(resolved)
    if not resolved.is_dir():
        msg = "no such plan file or directory"
        raise PlanError(msg, resolved)

    files = sorted(resolved.glob(f"*{PLAN_SUFFIX}"))
    if not files:
        msg = f"contains no {PLAN_SUFFIX} plan files"
        raise PlanError(msg, resolved)

    documents = [(file, _read_toml(file)) for file in files]
    # The merged document has no single file to blame, so scenario-level
    # errors report the directory. Per-file problems were raised above.
    return _validate(_merge(documents), resolved)
