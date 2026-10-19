# Configuration

GitWarden resolves settings in this order:

1. environment variables;
2. the TOML file at `$GW_DATA_DIR/config/gitwarden.toml` (or the path in `GW_CONFIG`);
3. built-in defaults.

The listen address has one extra level below the TOML file, `GW_DEFAULT_HOST`; see [Listen address](#listen-address).

In the container, `GW_DATA_DIR` defaults to `/data`. In a source checkout, the application default is `./data`. The TOML example in `data/config/gitwarden.toml.example` shows the same sections and defaults. The administrator password is the exception: it is read from `GW_ADMIN_PASSWORD` only and is never loaded from TOML.

## Container and process settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `GW_ADMIN_PASSWORD` | required | WebUI administrator password. Startup fails when it is empty or whitespace-only. Other passwords are preserved exactly, including surrounding whitespace. |
| `GW_DATA_DIR` | `/data` in the image, `./data` otherwise | SQLite database, archives, credentials, and work directory. |
| `GW_CONFIG` | `$GW_DATA_DIR/config/gitwarden.toml` | Optional TOML configuration path. |
| `GW_PORT` | `8080` | WebUI port and Compose host/container port; both default to `8080`. |
| `GW_HOST` | unset | Application listen address. Overrides `host` in `[server]`. See [Listen address](#listen-address). |
| `GW_DEFAULT_HOST` | `0.0.0.0` in the image, unset otherwise | Fallback listen address used when neither `GW_HOST` nor `[server] host` is set. Set by the Docker image; not meant to be set by hand. |
| `GW_TICK_SECONDS` | `30` | Scheduler polling tick. Must be greater than zero. |
| `GW_WORKERS` | `2` | Scheduler worker count, from 1 through 4. |
| `GW_MIN_SYNC_MINUTES` | `5` | Minimum interval accepted for a task. Minimum allowed setting is 1. |
| `GW_MIN_DISCOVERY_MINUTES` | `15` | Minimum interval accepted for a platform access. Minimum allowed setting is 1. |
| `GW_STALL_MINUTES` | `10` | Stops a Git operation after this many minutes without progress. It does not cap a progressing run. |
| `GW_LOG_RETENTION_DAYS` | `90` | Retention for run and activity records. Must be at least 1. |
| `GW_DELETE_GUARD` | `on` | Mass-deletion guard for the main mirror. Accepts `true`/`false`, `1`/`0`, `on`/`off`, `yes`/`no` (case-insensitive); any other value, including an empty string, stops startup. `off` disables every guard rule, including "every ref deleted" and "every branch deleted", and restores the old behavior where an emptied source also empties the main mirror. See [Mass-deletion guard](#mass-deletion-guard). |
| `GW_DELETE_GUARD_RATIO` | `0.5` | Hold the main mirror when one run would delete at least this fraction of its refs (greater than 0, at most 1). Deleting every ref, or every branch, is always held regardless of this value. |
| `GW_DELETE_GUARD_MIN_REFS` | `3` | The ratio rule applies only when at least this many refs would be deleted, so removing one branch from a small repository is not held. Must be at least 1. |

Compose also reads these project-environment values:

| Variable | Used by | Meaning |
| --- | --- | --- |
| `GW_BIND` | Compose | Host address for the WebUI port mapping in `compose.yml`; defaults to `127.0.0.1`. No effect with `docker run` or host networking. |
| `GW_IMAGE` | Compose | Image name. Defaults to the local `gitwarden:local` build. |

`GW_BIND` and `GW_IMAGE` are Compose interpolation inputs, not application settings. `GW_BIND` only fills in the host address of the port mapping in `compose.yml`; the application never reads it, so it has no effect with `docker run` (even with `-e GW_BIND=…` or `--env-file .env`) or with `--network host` / `network_mode: host`. Docker Compose loads `.env` in the project directory; the Python application does not automatically load that file. With plain Docker, pass the file, a plaintext file on disk, using `--env-file .env` or export the variables yourself.

## Listen address

The application chooses its listen address in this order (highest first):

1. `GW_HOST` environment variable;
2. `host` under `[server]` in the TOML file;
3. `GW_DEFAULT_HOST` environment variable, which the Docker image sets to `0.0.0.0`;
4. `127.0.0.1`.

An empty `GW_HOST` or `GW_DEFAULT_HOST` counts as unset. Each value must be an IP address (`127.0.0.1`, `0.0.0.0`, `::`; IPv6 without brackets; a zone id may only use `A–Z`, `a–z`, `0–9`, `_`, `.`, `-`) or an RFC 1123 host name (labels use letters, digits, and hyphens only—no underscores such as Docker service names like `my_host`). Startup stops before the WebUI or scheduler starts when a value is invalid; most invalid values are rejected here, for example `*`, `[::]`, a value with spaces or control characters, an IPv4-mapped IPv6 address (`::ffff:127.0.0.1`), a trailing-dot pseudo-address (`127.0.0.1.`), or a numeric shorthand such as `0` or `127.1`; the error names the source (`GW_HOST`, `GW_DEFAULT_HOST`, or `[server] host` with the TOML path). When `GW_HOST` overrides a different `[server] host`, startup logs a warning such as `警告: 环境变量 GW_HOST=0.0.0.0 覆盖了配置文件 /data/config/gitwarden.toml 中的 [server] host=127.0.0.1`; `GW_HOST` still wins. Listening on `0.0.0.0` or `::` also logs a startup warning that host networking would expose every host interface.

`"::"` listens on IPv6 only: IPv4 clients, including `127.0.0.1` and the Docker healthcheck, are refused.

- **Run directly** (`python -m gitwarden`, systemd, a process manager): GitWarden listens on `127.0.0.1` and is reachable from the same machine only.
- **Official image, Compose, or `docker run` with the default bridge network**: the application listens on `0.0.0.0` inside the container, which published ports require. Host exposure is controlled by the port mapping (`GW_BIND` in Compose, `--publish 127.0.0.1:8080:8080` with plain Docker). With plain Docker, the mapping must include `127.0.0.1` to stay local: `-p 8080:8080` without an address publishes on every IPv4 and IPv6 interface (`0.0.0.0` and `[::]`), and ports published by Docker bypass host firewall rules such as ufw or firewalld (filter in the `DOCKER-USER` chain or publish on a specific address). Do not set `GW_HOST` or `[server] host` to `127.0.0.1` in a container: the published port stops answering.
- **`--network host` (Compose: `network_mode: host`)**: the container shares the host's network, so there is no port mapping. The image default `0.0.0.0` then listens on **every host interface**, and `GW_BIND` and `--publish` have no effect. You **must** set `GW_HOST` or `[server] host` to restrict it (for example `GW_HOST=127.0.0.1`, or one interface address such as the NIC IP). Here `127.0.0.1` is the host's loopback, so it does not break access from the host. The image healthcheck connects to the address GitWarden actually listens on (`0.0.0.0`/`::` are probed via `127.0.0.1`), so binding to a NIC IP stays healthy. `GW_HOST` and `[server] host` are honored starting with image 20261019.2; earlier images (20261019.1, 20261016.1) ignore them and always listen on every host interface, so use 20261019.2 or later.

`GW_BIND` and `GW_HOST` are different settings: `GW_BIND` is the host-side address of the Compose port mapping; `GW_HOST` is the address the application itself listens on.

### Upgrading from earlier versions

Versions up to and including 20261019.1 always listened on `0.0.0.0` and ignored `GW_HOST` and `[server] host`; the listen-address settings take effect starting with 20261019.2. After upgrading:

- **Affected:** deployments that run GitWarden directly, or in a custom image or entrypoint that does not use this repository's `Dockerfile`, and are reached from another machine, from another network namespace on the same host (for example a reverse proxy, monitor, or uptime probe running in a container that connects to `172.17.0.1` or `host.docker.internal`), or, for a custom image, through a published port. They now listen on `127.0.0.1` only. `ss -ltnp | grep 8080` shows `127.0.0.1:8080` in that case. For containers on the same host, either set `GW_HOST` to the Docker bridge address (for example `172.17.0.1`; the bridge must exist before GitWarden starts) or set `GW_HOST=0.0.0.0` and block outside access with a firewall.
- **Not affected:** the official image, images built `FROM` it, and the repository's Compose file. With `--network host` the image listens on every host interface, as before; see above to restrict it.
- **Restore the old behavior:** set `GW_HOST=0.0.0.0` (for systemd, `Environment=GW_HOST=0.0.0.0`, then `systemctl daemon-reload` and restart the unit), or add `host = "0.0.0.0"` under `[server]`. A custom image can use `ENV GW_DEFAULT_HOST=0.0.0.0` instead, which keeps a mounted TOML `host` effective. Listening on all interfaces exposes the WebUI to every network the host is on; use a firewall or reverse proxy.

## TOML settings

Use a file such as `/data/config/gitwarden.toml` when a setting should remain with the data volume:

```toml
[server]
# host = "127.0.0.1"  # see Listen address; leave unset in Docker
port = 8080

[scheduler]
tick_seconds = 30
workers = 2
min_sync_minutes = 5
min_discovery_minutes = 15
stall_minutes = 10

[logs]
retention_days = 90

[safety]
delete_guard = true
delete_guard_ratio = 0.5
delete_guard_min_refs = 3
```

Environment variables override the corresponding TOML values. In `[safety]`, `delete_guard` takes a TOML boolean, the integers `0`/`1`, or one of the strings accepted by `GW_DELETE_GUARD`; `delete_guard_ratio` and `delete_guard_min_refs` must be TOML numbers (`delete_guard_min_refs` an integer), not strings. Unlike the numeric variables, `GW_DELETE_GUARD` set to an empty string is an error rather than "unset". Invalid numbers or values outside the validation range stop startup with an explanatory error. A task's sync interval and a platform access's discovery interval must also satisfy the configured minimum when saved in the WebUI.

## Scheduling behavior

Hand-created tasks default to a 60-minute sync interval. Platform-discovered tasks inherit the access's default sync interval, which is 60 minutes unless changed. Platform discovery also defaults to 60 minutes. These are task/access values; the `GW_MIN_*` settings are lower bounds.

After a run, GitWarden calculates the next due time from the run's end. The effective interval can be larger than the configured value when the recent successful runs are long, and repeated failures apply exponential backoff up to six hours. A successful run clears the failure backoff. A Git operation with progress may run for hours; `GW_STALL_MINUTES` only detects a no-progress stall.

## Mass-deletion guard

A fetch that succeeds but returns an empty or nearly empty source (for example, a repository that was deleted and recreated empty under the same name) would otherwise delete the matching refs from every main mirror. Before any push, GitWarden counts the main-mirror refs that the run would delete. The guard trips when any of these is true:

1. the run would delete **every** ref in the main mirror (always held, whatever the ratio and minimum);
2. the run would delete **every branch** (`refs/heads/*`) in the main mirror, however many tags remain (always held);
3. the run would delete at least `delete_guard_min_refs` refs (default 3) **and** at least `delete_guard_ratio` of the main mirror's refs (default 0.5; branches and tags are counted together).

When the guard trips, the whole main-mirror update for that target is held: neither the deletions nor any other update in the same run is pushed, so the mirror is never left half-updated. The local archive and the history repository still receive the preservation tags, and the run is recorded as `delete_guard` (**Mass deletion held**) without failure backoff. The run's Logs entry summarizes how many refs would have been deleted and names the first few. Later runs are held again for as long as the same refs are still missing from the source, and **Retry** on the target runs the same check, so it is held again too (and is recorded as `delete_guard`, not as a failure).

### How a hold clears

The guard keeps no state between runs. Every run recounts, **by ref name only**, the main-mirror refs whose names are missing from the source, and decides again from scratch. This has three consequences:

- **A ref counts as back as soon as a ref with the same name exists in the source, whatever it points to.** If the deleted branches are recreated under the same names with arbitrary or unrelated content, the next run is not held and the main mirror is force-updated (non-fast-forward) to the new commits. The old commits are not lost: they were pushed to the history repository in the held run, and the history gate still requires that before rewriting.
- **A partial recreation can release the rest.** If only some refs come back and the deletions that remain fall below the thresholds (not every ref, not every branch, and fewer than `delete_guard_min_refs` or below `delete_guard_ratio`), those remaining deletions are applied in that run without any confirmation.
- **Nothing marks the release.** The run that finally changes the main mirror is an ordinary `ok` run; the delete events were recorded in the held run, and no event says that the hold was cleared.
- **An external change to the main mirror while held becomes a conflict.** If something else rewrites the main mirror during the hold, the next run reports `conflict` instead of `delete_guard` until that conflict is resolved; after that, the guard may trip again on the same missing refs.

Remembering the held set (and releasing only when the same commits come back, or on an explicit confirmation) is planned as a follow-up together with the per-target release action.

### Releasing a held target

First check the source: was it emptied, recreated, renamed, or is the task pointing at the wrong repository? Then choose one of the following:

- **(a) The source recovers.** Restore the source (or fix the task's source URL). The next run passes the guard and syncs normally; nothing else is needed. Because only names are counted (see above), check that the restored refs have the content you expect: same-name refs with different content are accepted and rewrite the mirror.
- **(b) The deletion was intended.**
  1. For a partial deletion (rule 3), temporarily raise `GW_DELETE_GUARD_RATIO` (`delete_guard_ratio` in `[safety]`) above the share being deleted, or raise `GW_DELETE_GUARD_MIN_REFS`. When every ref or every branch is deleted (rules 1 and 2), raising the ratio does not help: set `GW_DELETE_GUARD=off` instead.
  2. Restart GitWarden so that the setting is read.
  3. Run the task once (**Run now**, or wait for the schedule) and confirm that the target is healthy.
  4. Restore the original setting and restart again.

If the deletion includes the target's default branch and the target refuses to delete it (Gitea and Forgejo default branches, or a bare repository with the default `receive.denyDeleteCurrent`), the release run deletes the other refs but reports `conflict` for that branch; change the target's default branch first, or leave that branch in place.

**Cost:** these settings are global. While the guard is off, **no task** has mass-deletion protection, not only the one you are releasing; while only the ratio or minimum is raised, every task keeps rules 1 and 2 but has a weaker rule 3. Any other source that is emptied during that window will empty its main mirror (the deleted refs are still preserved in the history repository). Keep the window short and do not forget step 4. A per-target "accept this deletion" action is planned as a follow-up and is not part of this release.

### False positives

- Legitimately renaming three or more branches in one go (for example `b1..b3` to `renamed-b1..3` in a 4-ref repository) deletes the old names and is held when it reaches the ratio. The new names are held along with everything else in that run.
- Renaming every branch, including renaming the only branch of a repository (for example `master` to `main`) while tags remain, is held by rule 2.
- A one-shot cleanup that removes at least half of the refs and at least three of them (for example 10 of 15 merged branches) is held.

Release these with option (b) above.

### Known bypasses

The guard is a tripwire for an emptied or mostly emptied source, not a complete defense against a hostile source. These cases are **not** held:

- **Gradual deletion:** deleting a few refs per run, each run below the thresholds (fewer than `delete_guard_min_refs` refs, or less than `delete_guard_ratio` of the mirror), until one branch is left. Every earlier deletion is applied; only the run that would delete the last remaining branch (rule 2) or the last remaining ref (rule 1) is held.
- **Placeholder first:** creating a new branch, letting one run copy it to the main mirror, then deleting the original refs over several runs, each run below the thresholds; because the placeholder branch stays, the mirror never loses every ref or every branch. (Deleting at least `delete_guard_min_refs` original refs and at least `delete_guard_ratio` of the mirror in one run is still held by rule 3. If the placeholder first appears in the same run that deletes every original branch, rule 2 still holds that run, and rule 1 as well when there are no tags.)
- **Dilution with junk tags:** adding many tags first so that later branch deletions stay under the ratio. Deleting *every* branch is still held by rule 2, but deleting all but one branch is not.
- **Very small repositories:** with only two refs (for example `main` and `dev`), deleting one is below `delete_guard_min_refs`, so a recreated repository that keeps one ref name passes.
- **Force-push to unrelated history:** rewriting branches is not a deletion and is never counted by the guard.

In every one of these cases the old objects are still preserved: each deleted or rewritten ref gets a `gw/<ULID>` preservation tag in the local archive and in the history repository before the main mirror changes, and end-to-end tests of each case found every original commit in the history repository. Recover from there as described in [Backup and recovery](Backup-and-recovery.md).

### Turning the guard off

`GW_DELETE_GUARD=off` (or `0`, `false`, `no`, or `delete_guard = false`) disables **all** guard rules, including "every ref deleted" and "every branch deleted". There is no separate switch that keeps only the "delete everything" protection. Leave the guard on unless you are releasing a held target as described above.

## Data layout

The application data directory contains:

```text
/data/
├── gitwarden.db          # SQLite metadata and logs
├── archives/*.git        # bare local Git archives
├── config/               # optional TOML configuration
└── work/                 # temporary credential and bundle work files
```

The archive and SQLite database are both required for a complete local recovery. Credentials are stored in the database and are needed to contact private sources and targets; protect the volume and the `.env` file as secrets.

## WebUI language and logging

The WebUI starts in English and has a Simplified Chinese switch. Each task can enable detailed Git logging. Errors always retain redacted output; successful runs retain full redacted Git output only when detailed logging is enabled. One raw output record is limited to 64 KiB. Log retention removes run and activity records, not preservation events, archives, or Git objects.
