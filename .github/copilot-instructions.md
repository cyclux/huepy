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
- NO `Any` type - always specify concrete types
- Always include return types, including `None`

## Documentation (REQUIRED)

- Google-style docstrings for all modules, classes, functions
- Module docstring with purpose + "Typical usage example:" for major components
- Include Args, Returns, Raises sections where applicable

## Logging & Output

- NEVER use `print()` - use logging module only
- Create module logger: `logger = get_logger(__name__)`
- Use appropriate levels: DEBUG, INFO, WARNING, ERROR, CRITICAL
- Include context in log messages

## Error Handling

- Create custom exceptions inheriting from base service exception
- Include HTTP status codes for web exceptions
- Provide detailed error context
- Use structured error dictionaries for validation

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

- Use Pydantic dataclasses (prefer dataclasses over BaseModel)
- Type all configuration values
- Provide sensible defaults
- Support environment separation

## Testing

- Focus on meaningful tests over coverage percentage
- Descriptive test names explaining scenarios
- Arrange-Act-Assert structure
- Mock external dependencies
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