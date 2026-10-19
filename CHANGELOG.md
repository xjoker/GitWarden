# Changelog

## 20261019.1 — 2026-10-19

- Run the default Compose service with a read-only root filesystem, a tmpfs `/tmp`, all Linux capabilities dropped, and `no-new-privileges`; only the `/data` volume is writable.
- Upgrade the runtime image's system packages at build time and remove the base image's standalone `pip`, which the runtime does not use.
- Document mounting a local source repository into the container, including ownership, read-only mounts, and repositories that are still pushed to on the host.
- Fix release details in the README, document the safety-gate hold, and add a demo GIF, issue templates, and a private vulnerability reporting entry point.

## 20261016.1 — 2026-10-16

First public release.

- Pin GitHub Actions dependencies to verified full commit SHAs.
- Skip tasks deleted before worker execution without recording a failed run.
- Reject empty and whitespace-only administrator passwords while preserving valid passwords exactly as configured.
- Keep simultaneous administrator sessions available across devices.

Includes the backup, preservation, recovery, discovery, and WebUI features described in the release candidate below. Git LFS payloads and submodule repositories are not backed up, and databases from development snapshots are not migrated.

## 20261010.1 — 2026-10-10

First public release candidate.

- Licensed under Apache License 2.0, included in source and container distributions.
- One-way Git backups over HTTPS, SSH, or local Git paths, with multiple targets per source.
- Preserve captured history before following force pushes, rewritten tags, and deleted references. Keep preserved objects locally and in a separate target history repository.
- Require empty target repositories at binding time and verify preserved history before replacing target references.
- Download recovery bundles and audit target history.
- Optional Gitea and Forgejo discovery, include/exclude rules, and target provisioning.
- Shared scheduling with bounded concurrency, adaptive intervals, failure backoff, and inactivity detection.
- English and Simplified Chinese WebUI, task status, run details, and activity logs.
- English-first documentation, deployment guides, Wiki sources, and screenshots using demonstration data.
- Container builds for Linux amd64 and arm64, Docker Compose and Docker deployment, and GitHub Actions validation and image publishing.
- Redact credentials inside serialized status parameters before storing logs.
- Keep target bindings during credential rotation and show newly provisioned targets as pending until their first sync.
- Fix narrow-screen credential layout, rule-mode labels, and task editing navigation.

Git LFS payloads and submodule repositories are detected but are not backed up. Only Git history already captured by GitWarden can be preserved. This release does not migrate databases from development snapshots.
