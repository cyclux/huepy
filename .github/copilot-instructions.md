---
applyTo: '**'
---

## Core Rules

- Apply DRY principle - eliminate duplication through abstraction
- Write explicit, self-documenting code
- Use composition over inheritance
- Follow type safety first approach

## Type Annotations (REQUIRED)

- Add type hints to ALL functions, class attributes, variables
- Use specific types: `dict[str, str]` not `dict`
- Use modern syntax: `list[str]` not `List[str]`
- Use `Any` only at the raw JSON boundary, then narrow it immediately
- Always include return types, including `None`

## Documentation (REQUIRED)

- Google-style docstrings for all modules, classes, functions
- Module docstring with purpose + "Typical usage example:" for major components
- Include Args, Returns, Raises sections where applicable

## Logging & Output

- Library code uses `logging.getLogger(__name__)`; it never configures host logging
- Runnable scripts in `examples/` may use `print()` for user-facing output
- `examples/` is type-checked with `src` and `tests`; only `reportUnusedCallResult` is relaxed there
- Use appropriate levels: DEBUG, INFO, WARNING, ERROR, CRITICAL
- Include context in log messages

## Error Handling

- Reuse the `HueError` hierarchy; add a subtype only for a distinct caller action
- Include HTTP status codes for web exceptions
- Provide detailed error context
- Parse response-body errors through `HueResponse` and `HueErrorDetail`

## Function/Class Design

- Avoid large classes and functions
- Only use classes when necessary (e.g. stateful objects)
- Prefer a functional programming style
- Single responsibility principle
- Small, focused functions
- Use dependency injection
- Prefer factory functions for complex object creation
- Avoid deep inheritance hierarchies

## Naming & Variables

- Descriptive names explaining purpose
- UPPER_CASE for constants
- Leading underscore for private members
- Avoid abbreviations except industry standard

## Imports

- Order: standard library → third-party → local
- Use absolute import paths
- Prefer explicit imports over wildcards
- Use qualified names when needed for clarity

## Comments

- Explain WHY, not WHAT
- Focus on business logic, algorithms, non-obvious decisions
- Only add when providing genuine insight

## Configuration

- `HueConfig` is a standard-library dataclass; payload and record models use pydantic
- Type all configuration values
- Provide sensible defaults
- Support environment separation

## Testing

- Focus on meaningful tests over coverage percentage
- Descriptive test names explaining scenarios
- Arrange-Act-Assert structure
- Fake the `Transport` protocol with `tests/conftest.py`; do not mock `aiohttp`
- Use pytest framework

## Performance

- Use async/await for I/O operations
- Context managers for resource cleanup
- Choose efficient data structures
- Implement lazy loading when appropriate

## Code Organization

- Logical module boundaries
- Resource-based routing for APIs
- Separate business logic from framework code
- Centralized configuration

## huepy Architecture

- Keep `aiohttp` confined to `src/huepy/client/http.py` and `src/huepy/client/discovery.py` (the
  latter runs before any bridge session exists); other modules depend on the `Transport` protocol
  in `client/protocol.py`. Runtime deps are `aiohttp`, `pydantic`, `zeroconf` (mDNS only)
- Keep `resources/` independent of `client/base.py` to preserve the acyclic import graph
- TLS is verified against Signify's bundled root CAs by default (`client/tls.py`), pinning the
  bridge-id common name when known; `TlsMode.INSECURE` is the explicit opt-out. Writes are paced
  to the bridge's budget in `client/ratelimit.py`, gated at the top of `HueHttpClient._request`
- Parse resource payloads through tolerant `HueModel` subclasses (`extra="allow"`)
- Route v2 envelopes through `unwrap()` / `raise_for_errors()`; HTTP 207 is a transport success
  whose `errors[]` still needs classification
- Keep ordinary handler reads uncached. Maintained local state is explicit and opt-in through
  `Hue(state=True)`
- Keep `recording/` depending only on `state/records.py` and the models; sinks receive enriched
  records, never the state graph, and own their own blocking work
- One logical light command is one PUT composed by `build_light_payload()`
- The authoritative v2 CLIP reference is the gated `developers.meethue.com` portal. Follow
  `docs/hue-portal-access.md`: a human solves the Cloudflare Turnstile once in the Playwright MCP
  browser, then `curl` reads every page with the exported session into `docs/hue-dev-docs/`
  (git-ignored). Credentials live in `.env` (git-ignored); never automate the challenge or commit
  the session

## Declarative plans

`huepy/plans/` reads TOML plan files and runs them. `PLANS.md` records the
format and the reasoning; read it before touching the format, the scheduling
maths, or the executor.

- **TOML only.** YAML 1.1 coerces `on`/`off`/`yes`/`no` to booleans, and `on` is
  the format's central key. TOML also rejects duplicate keys, so a copy-pasted
  `at` cannot silently drop a step. `tomllib` is stdlib; adding a YAML
  dependency needs a better reason than taste.
- **Plan models set `extra="forbid"`**, inverting `HueModel`. An unknown key in
  a bridge payload is new firmware; an unknown key in a hand-written config is a
  typo, and ignoring it is the failure the format exists to prevent.
- **`fields`, `schema`, `sun`, `timeline` and `arbiter` are pure** -- no clock,
  no client, no I/O; `now` is always a parameter. That is what lets a simulated
  day run in microseconds and what makes crash recovery work. Keep them that way.
- **A fade is one PUT, not a tick loop.** The bridge runs a transition up to
  6000 s (`MAX_TRANSITION_MILLISECONDS`, measured, not documented). Longer ramps
  chain segments in `executor.py`; nothing re-asserts on a timer.
- **Interpolation is for catching up only.** Normal ticks send the step's final
  target with the remaining ramp and let the bridge do the fade; `target_at()`
  answers the restart question, `current_step()` the scheduling one.
- **Override detection cannot use the state layer's time window.** A 100-minute
  fade would mask a human for the whole ramp, so `arbiter.Fade.explains()`
  compares a report against the fade's own interpolated expectation instead.
  `Change.origin == "self"` *is* that window; the runner skips only
  `observation == "command_echo"` and judges everything else.
- **Every trigger is one path.** Sensors and signals alike reach
  `Arbiter.fire(key, now)` as the selector string they were written as; do not
  add a second dispatch. What a kind *means* lives in `runner._edge()` and,
  for a level crossing, `runner._level_edge()`; the runner's `_levels` dict is
  its only per-sensor memory.
- **Handing a scope back never snaps.** The return to a day curve is floored at
  `catchup_ramp`; a mode keeps its author's ramp. `Claim.source` vs
  `ScopeState.owner.source` is what tells a hand-over from a claim still in
  force, and `Claim.since` vs `ScopeState.yielded_at` is what ends a yield --
  never a precomputed resume time. A hand change is remembered in
  `ScopeState.reported` and is where the next fade starts.
- `plans/` depends on the `PlanClient` Protocol in `plans/protocol.py`, never on
  `client/base.py`. `src/huepy/cli.py` is the composition root: the one module
  outside `client/` that binds a plan to a concrete `Hue`.
- Sun times are computed in-process: `geolocation` reports a sunset but never a
  sunrise, its coordinates are write-only, and `smart_scene` timeslots accept
  neither sunrise nor offsets. Plans are not compiled to the bridge.
