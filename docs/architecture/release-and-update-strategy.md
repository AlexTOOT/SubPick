# SubPick Release and Update Strategy

## Decision

The first public release uses one Git repository and one immutable Docker image.
The repository contains:

- the SubPick service;
- the 拾幕 Web UI;
- built-in Provider adapters;
- database migrations, tests, deployment files, and documentation.

Do not split the Web UI or built-in Provider adapters into separate repositories
before the first stable release. Independent repositories would require an adapter
package installer, compatibility matrix, release coordination, rollback design, and
signature or trust policy. Those costs do not improve the current single-maintainer
product.

The Provider adapter interface remains a clear internal boundary. A future adapter
may become an independently versioned Python package only after there is a real need
to install it without updating the main image.

## Version Ownership

The SubPick version identifies the complete deployable image. The image pins the Web
UI, built-in adapters, Subliminal, ffsubsync, and other runtime dependencies through
`uv.lock`.

Diagnostics should show:

- SubPick version;
- built-in adapter versions;
- Subliminal and ffsubsync versions;
- available upstream versions when an update check succeeds.

Component update checks are informational. Do not run `pip install` inside a running
container: the result is not reproducible, disappears when the container is replaced,
and can create combinations that were never tested together.

## GitHub Automation

The repository includes two GitHub Actions workflows:

1. **Checks**: run on pull requests and pushes; install from `uv.lock`, run the test
   and check scripts, and build the Docker image without publishing it.
2. **Release**: run for a version tag such as `v0.5.0`; build Linux `amd64`
   and `arm64` images, publish a multi-architecture manifest to GHCR, and tag it with
   the exact version. Add `latest` only for stable releases.

The release workflow must build the committed source state and must not inject user
configuration or credentials.

## Update Experience

The first release should support:

- checking whether a newer SubPick image/version exists;
- showing the release notes and target version;
- showing or copying the deployment-specific upgrade command;
- polling health after the user performs the upgrade.

Do not mount the Docker socket into SubPick merely to add an Update button. Docker
socket access effectively grants host-level control to the web application.

A future one-click update may use an optional, narrowly scoped updater service. Its
contract must allow only the labelled SubPick deployment to pull an approved image
tag and recreate itself. It must require a separate token, keep the previous image
available for rollback, and expose progress while the main container restarts. This is
release infrastructure, not a requirement for the first functional release.

## Data and Rollback

User-owned `config` and `data` directories are never included in an image update.
`cache` is rebuildable and does not need routine backup.

For the first public release, document how to back up `config` and `data`. A full
upgrade-from-old-public-version test is not required until there is a second public
release. Existing additive SQLite migrations remain covered by automated tests.
