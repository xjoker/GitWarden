# Changelog

## Unreleased

## 20261020.1 — 2026-10-20

Healthcheck follows the real listen address, warn when listening on all interfaces, tighten host validation, and small follow-ups from #10 / #13 / #14 reviews.

- Bypass HTTP proxies for Docker healthchecks so proxy settings cannot replace the local service's health result.
- Docker `HEALTHCHECK` probes the address GitWarden actually listens on (via `load_config` / `probe_health`); `0.0.0.0` is checked through `127.0.0.1` and `::` through `[::1]`, so binding to a NIC IP under `--network host` no longer stays unhealthy. `GW_DEFAULT_HOST=0.0.0.0` inside the official image is unchanged.
- Log a startup warning when the process listens on `0.0.0.0` or `::` (host networking would expose every host interface; on the default bridge, exposure is still controlled by `-p` / `GW_BIND`).
- Reject more invalid listen hosts before bind: IPv6 zone ids limited to `[A-Za-z0-9_.-]`, IPv4-mapped IPv6 (`::ffff:…`), and hostnames whose rightmost label is all-numeric; document that hostnames follow RFC 1123 (no underscores). Bind failures name the listen address.
- Favicon / apple-touch links in the WebUI add `?v=<VERSION>` so browsers pick up icon changes without waiting for `max-age`.
- Partial runs that both hold mass-deletion and fail other targets mention the failure count in the run message; document TOML `delete_guard` accepting integers `0`/`1`, external mirror conflicts during a hold, and assert archive locks are released after a stalled run.

## 20261019.2 — 2026-10-19

Configurable listen address (`GW_HOST` is now honored): direct runs default to `127.0.0.1`, while the official image still listens on `0.0.0.0` inside the container. Also adds the GitWarden logo and favicon.

- Listen on `127.0.0.1` instead of `0.0.0.0` when run directly. New settings `GW_HOST` and `[server] host` choose the listen address (`GW_HOST` wins, and startup logs a warning when it overrides a different `[server] host`). New `GW_DEFAULT_HOST` is the fallback used when neither is set; the official image sets it to `0.0.0.0`. An invalid value in `GW_HOST`, `[server] host`, or `GW_DEFAULT_HOST` stops startup and names the setting.
- Add the GitWarden logo: replace the WebUI sidebar icon, serve `favicon.ico`, `favicon.svg` (with a dark-scheme variant), and `apple-touch-icon.png` from three fixed routes that are reachable without logging in, and add the logo source files under `docs/brand/`.

Upgrade notes for the listen address: 20261019.1 and earlier ignored `GW_HOST` and always listened on `0.0.0.0`; from this release `GW_HOST` (then `[server] host`) is honored everywhere, including in the container. Containers from the official image keep listening on `0.0.0.0` inside the container when neither `GW_HOST` nor `[server] host` is set, so `docker run -p 127.0.0.1:8080:8080 …` and the repository's `compose.yml` (host side `${GW_BIND:-127.0.0.1}`) keep working; host exposure is still chosen by the `-p` mapping or `GW_BIND`. A `GW_HOST=127.0.0.1` (or `[server] host = "127.0.0.1"`) that had no effect in 20261019.1 now takes effect and makes a published port unreachable (the container still reports healthy, because the healthcheck connects from inside the container); remove it, or set `GW_HOST=0.0.0.0` together with a `-p` mapping to expose the WebUI. Deployments that run GitWarden directly and are reached from another machine, or from a container on the same host, stop answering after the upgrade; set `GW_HOST` or `[server] host` to `0.0.0.0` to accept remote connections again. Custom images or entrypoints not built from this repository's `Dockerfile` are affected too; images `FROM` the official image are not. Otherwise the official image and Compose are unaffected: the image sets `GW_DEFAULT_HOST=0.0.0.0`, which an explicit `GW_HOST` or `[server] host` overrides. With `--network host` (Compose: `network_mode: host`) there is no port mapping, so `-p` and `GW_BIND` have no effect and the image still listens on every host interface; set `GW_HOST=127.0.0.1` or `host = "127.0.0.1"` under `[server]` to restrict it (earlier images ignore both).

## 20261019.1 — 2026-10-19

Mass-deletion guard, container hardening, and documentation updates.

- Hold the main mirror when a source that still answers has become empty or has lost most of its refs: when one run would delete every ref, every branch, or at least half of the refs and at least three of them (`GW_DELETE_GUARD_RATIO`, default `0.5`; `GW_DELETE_GUARD_MIN_REFS`, default `3`), nothing is pushed to that main mirror, the deleted refs are still preserved in the history repository, and the target shows **Needs attention** (`delete_guard`). The hold clears once refs with the same names are back in the source. To release an intended deletion, temporarily raise `GW_DELETE_GUARD_RATIO` or `GW_DELETE_GUARD_MIN_REFS` (partial deletions only) or set `GW_DELETE_GUARD=off` (required when every ref or every branch is deleted; it disables every rule), restart, let one run complete, then restore and restart. The settings are global, so the relaxation applies to every task meanwhile. A source that cannot be fetched is now shown as **source failing** instead of healthy.
- Run the default Compose service with a read-only root filesystem, a tmpfs `/tmp`, all Linux capabilities dropped, and `no-new-privileges`; the application writes only to `/data` and the `/tmp` tmpfs (mounted `noexec`).
- Upgrade the runtime image's system packages at build time and remove the base image's standalone `pip`, which the runtime does not use.
- Document mounting a local source repository into the container, including ownership, read-only mounts, repositories that are still pushed to on the host, and running the container as the repository owner.
- Fix release details in the README, document the safety-gate hold, and add a demo GIF, issue templates, a private vulnerability reporting entry point, and a command for trying the published image.

Upgrade notes for the Compose hardening: it applies only when you use the repository's `compose.yml`; plain `docker run` deployments are unchanged. Keep `/data` on a volume: with a read-only root filesystem and no volume at `/data`, startup fails with `EROFS` (`Read-only file system: '/data/gitwarden.db'`). Custom Compose overrides that rely on root privileges, such as `user: 0` writing to the UID 10001-owned `/data`, lose `CHOWN` and `DAC_OVERRIDE` under `cap_drop: ALL`, and any write outside `/data` and `/tmp` fails; adjust such overrides before upgrading.

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
