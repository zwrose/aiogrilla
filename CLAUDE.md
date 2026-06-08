@AGENTS.md

## Claude-specific notes
- Run the test suite with `.venv/bin/pytest -q` before committing.
- Lint/type: `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`, and `.venv/bin/pyright`.
- Never read or commit real Grilla account credentials; backend IDs in `const.py` are client-side app constants, not secrets.
- **Commit messages MUST be Conventional Commits** (`feat:`, `fix:`, `docs:`, `chore:`, `feat!:`/`BREAKING CHANGE:` …); release-please derives versions and the changelog from them. See `AGENTS.md` / `CONTRIBUTING.md`.
