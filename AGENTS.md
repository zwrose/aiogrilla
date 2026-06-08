# AGENTS.md — AI assistant guidance for aiogrilla

## Project purpose

Unofficial async Python client for Grilla Grills Alpha Connect smokers (read-only,
v1 scope: live temperatures, cook status, cook timer).

## Layout

```
src/aiogrilla/
    const.py       — backend URLs, pool IDs, and other client-side constants
    models.py      — dataclasses (Grill, GrillState) and enums (Mode, CookMode, TemperatureUnit)
    auth.py        — AWS Cognito authentication and IAM credential exchange
    discovery.py   — HTTP call to list grills for the authenticated account
    mqtt.py        — AWS IoT / MQTT streaming (IotStream)
    client.py      — GrillaClient: public orchestration layer
    exceptions.py  — GrillaError, GrillaAuthError, GrillaConnectionError

tests/
    fixtures/      — JSON payload samples used by unit tests
    test_*.py      — unit + integration tests (no live network)
```

## Hard boundary

**This library MUST NEVER import `homeassistant`.** It is a standalone async
library. Any Home Assistant integration should live in a separate package that
depends on aiogrilla, not the other way around.

## Running checks

```bash
.venv/bin/pytest -q          # run the test suite
.venv/bin/ruff check .       # lint
.venv/bin/pyright            # type-check
```

All three must pass cleanly before committing or opening a PR.

## Secrets and constants

Constants in `const.py` (User Pool IDs, IoT endpoints, client IDs) are
client-side, non-user-specific constants. They are not personal secrets and may
be committed. Real Grilla account
credentials (email, password, refresh tokens, IAM keys) are secrets and must
**never** be committed, logged, or included in test fixtures.

## TDD expectation

Write tests before or alongside new code. New parsing logic, auth flows, or
protocol handling should be covered by unit tests using fixture payloads or
mocks — no live network calls in the test suite.

## Commit messages — Conventional Commits (required)

Every commit on `main` and every squash-merged PR title MUST be a
[Conventional Commit](https://www.conventionalcommits.org/). This is load-bearing:
`release-please` derives the version bump and `CHANGELOG.md` from these messages.

- `feat:` → minor bump · `fix:`/`perf:` → patch bump · `docs:`/`refactor:`/`test:`/`chore:`/`ci:`/`build:` → no release
- `feat!:` or a `BREAKING CHANGE:` footer → major bump (minor while pre-1.0)
- Examples: `feat: expose probe 2 target temperature`, `fix(mqtt): reconnect after credential refresh`

See `CONTRIBUTING.md` for the full table. Do not write non-conventional commit
messages in this repo.
