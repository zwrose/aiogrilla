# Contributing to aiogrilla

Thanks for your interest in improving aiogrilla! This is a small, focused
async client library. Contributions of all sizes are welcome.

## Development setup

```bash
git clone https://github.com/zwrose/aiogrilla.git
cd aiogrilla
uv venv
uv pip install -e ".[dev]"
```

## Checks that must pass

All four gates run in CI and must pass locally before you open a pull request:

```bash
ruff check .
ruff format --check .
pyright
pytest -q
```

Installing the pre-commit hooks runs the formatters and linters automatically:

```bash
pre-commit install
```

## Tests

Write tests alongside new code. New parsing, auth, or protocol handling should be
covered by unit tests using fixture payloads or mocks — the suite never makes
live network calls. If you report a parsing problem, please include a **redacted**
payload sample (no account identifiers or tokens).

## Commit messages — Conventional Commits (required)

This repository uses [Conventional Commits](https://www.conventionalcommits.org/).
**This is required, not stylistic:** releases and the `CHANGELOG.md` are generated
automatically by [release-please](https://github.com/googleapis/release-please)
from commit messages. A commit that does not follow the format will not be
reflected correctly in the changelog or version bump.

Format:

```
type(optional-scope): short summary
```

Common types and their release effect:

| Type | Use for | Version effect |
|------|---------|----------------|
| `feat:` | a new capability | minor bump |
| `fix:` | a bug fix | patch bump |
| `docs:` | documentation only | none |
| `refactor:` | non-behavioral code change | none |
| `test:` | tests only | none |
| `chore:` / `ci:` / `build:` | tooling, CI, packaging | none |
| `perf:` | performance change | patch bump |

A `!` after the type (e.g. `feat!:`) or a `BREAKING CHANGE:` footer marks a
breaking change (major bump; while pre-1.0, treated as a minor bump).

Examples:

```
feat: expose probe 2 target temperature
fix(mqtt): rebuild the stream after a credential refresh
docs: clarify the install steps
chore: bump ruff to 0.6
```

If you open a PR with multiple commits, the **PR title** must also be a valid
Conventional Commit, because PRs are squash-merged using the title.

## Pull requests

- Keep changes focused; one logical change per PR.
- Make sure the four checks above pass.
- Update or add tests for behavior changes.
- The maintainer reviews and merges; thanks for your patience.
