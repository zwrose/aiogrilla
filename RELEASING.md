# Releasing aiogrilla

Releases are automated with [release-please](https://github.com/googleapis/release-please)
and published to PyPI via **Trusted Publishing**. Do not bump the version or edit
the changelog by hand.

## How it works

1. Land commits on `main` using [Conventional Commits](https://www.conventionalcommits.org/)
   (`feat:`, `fix:`, `feat!:` …) — see `CONTRIBUTING.md`.
2. release-please keeps an open **release PR** that bumps `pyproject.toml`, updates
   `CHANGELOG.md`, and computes the next SemVer from the commits.
3. **Merge the release PR.** release-please tags `vX.Y.Z` and creates a GitHub Release.
4. The `release: published` event triggers `.github/workflows/publish.yml`, which
   builds the sdist + wheel and uploads them to PyPI via OIDC.

## Versioning

SemVer. While pre-1.0, breaking changes may ship in a minor bump.
`aiogrilla.__version__` reads the installed package metadata.
