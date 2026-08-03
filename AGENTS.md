# Development Rules

## Toolchain

- Use Python 3.12 only. The project runtime is selected by `.python-version`.
- Use `uv` and the repository-local `.venv`; never run project commands with system
  `python`, `pip`, or a global virtual environment.
- Run `.\scripts\bootstrap.ps1` before development. Follow the testing strategy below
  instead of running the full suite after every small change.
- Keep pytest temporary data under `.tmp`; do not use the system temporary directory.

## Testing Strategy

- Match verification effort to the change. Do not run the full test suite after every
  small edit.
- For copy, CSS, and small frontend-only changes, run relevant WebUI tests and inspect
  the affected page at representative desktop widths.
- For an isolated adapter or feature, run its test module plus directly related
  registry, scoring, or API tests.
- For database, queue, state-machine, task-pipeline, concurrency, or shared-contract
  changes, run focused tests while iterating and run `.\scripts\check.ps1` before the
  final commit or NAS deployment.
- Before a release, always run `.\scripts\check.ps1` and a container smoke test.
- Most automated tests use mocks and must not consume live provider quotas. Run live
  provider or NAS tests only when the behavior specifically requires them.

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
