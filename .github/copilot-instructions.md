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
