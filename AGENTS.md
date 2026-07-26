# Development Rules

## Toolchain

- Use Python 3.12 only. The project runtime is selected by `.python-version`.
- Use `uv` and the repository-local `.venv`; never run project commands with system
  `python`, `pip`, or a global virtual environment.
- Run `.\scripts\bootstrap.ps1` before development, `.\scripts\test.ps1` for tests,
  and `.\scripts\check.ps1` before committing.
- Keep pytest temporary data under `.tmp`; do not use the system temporary directory.

## Dependencies

- `uv.lock` is the reproducible dependency source for local development and Docker.
- After changing `pyproject.toml`, run `.\scripts\update-dependencies.ps1` and commit
  `pyproject.toml` and `uv.lock` together.
- Docker builds must use `uv sync --frozen`; do not add unpinned `pip install` steps.
- Missing optional tools must produce a diagnostic status instead of crashing an API.

## Deployment

- Deploy committed Git snapshots with `.\scripts\deploy-nas.ps1`.
- Never package or overwrite NAS `config`, `data`, `cache`, compose files, or media.
- Do not store API keys, tokens, SSH key contents, or local credentials in Git.

## Local Tools

- Prefer `git` and `uv` from `PATH`.
- On Windows, the helper scripts also detect Scoop installations under
  `%USERPROFILE%\scoop`.
