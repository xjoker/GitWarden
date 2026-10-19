# GitWarden

> Mirror the present. Preserve the past.

GitWarden is a self-hosted Git history mirror. It periodically fetches a source repository into a local archive and pushes the collected state to one or more writable Git targets. It does not require a hosting-platform API for the core workflow, so a source or target can be any supported Git URL, including a manually configured GitHub repository.

English · [简体中文](README.zh-CN.md)

![Terminal demo: a force push while the history repository is offline holds the main mirror, and the old commit stays preserved](docs/demo.gif)

*Demo (about 50 seconds, local bare repositories): GitWarden mirrors a source; the history repository then rejects pushes and the source force-pushes `main` and fast-forwards `dev`. The safety gate holds the whole main mirror (both branches), the old `main` commit stays in the local archive as `refs/preserved/<ULID>`, and once the history repository accepts pushes again the preservation tag is confirmed first and the mirror catches up. `gwctl` in the recording is a small demo helper script that submits the same WebUI forms as the backup wizard and the **Run now** button; GitWarden does not ship a CLI.*

![GitWarden overview](docs/images/overview.jpg)

## What it protects

Each backup task has one source, a durable local archive, and one or more targets. A target has two repositories:

- a **main mirror** containing the current `refs/heads/*` and `refs/tags/*` state;
- a private **history repository** containing GitWarden's `gw/*` preservation tags.

When a collected branch is force-pushed or deleted, or a collected tag is rewritten, GitWarden preserves the old Git object in the local archive before changing its mirror references. It then publishes that preserved object to the history repository before the main mirror update is allowed. A normal fast-forward needs no extra preservation tag. The process is automatic; there is no approval queue for ordinary Git changes.

A source that still answers but has suddenly become empty, or has lost most of its refs, is treated as suspicious rather than as an instruction to empty the backup. When one run would delete every ref from a main mirror, every branch (however many tags remain), or at least half of its refs and at least three refs (configurable through `GW_DELETE_GUARD_RATIO` and `GW_DELETE_GUARD_MIN_REFS`), GitWarden still preserves the deleted refs in the local archive and the history repository, but leaves the main mirror completely unchanged for that run and marks the target **Needs attention** (`delete_guard`). A source that cannot be fetched at all (404, 401, path missing) never deletes anything; while it keeps failing, its targets are shown as **source failing** instead of healthy.

What to know about the mass-deletion guard (details in [Configuration → Mass-deletion guard](docs/wiki/Configuration.md#mass-deletion-guard)):

- **Release:** the guard counts ref names only and keeps no state. As soon as refs with the same names are back in the source, whatever they point to, the next run passes and the main mirror is rewritten to them. If only some come back and the remaining deletions fall below the thresholds, those deletions are applied in that run. The old commits stay in the history repository. If the deletion was intended, temporarily raise `GW_DELETE_GUARD_RATIO` or set `GW_DELETE_GUARD=off` (only `off` helps when every ref or every branch is deleted), restart, let one run complete, then restore the setting and restart. **Retry** alone is held again.
- **Cost:** the setting is global. While it is `off`, no task has mass-deletion protection; while only the ratio is raised, every task keeps the every-ref and every-branch rules but has a weaker partial-deletion rule.
- **False positives:** legitimately renaming three or more branches at once, renaming every branch, or a large one-shot cleanup is held.
- **Not caught:** gradual deletion, creating a placeholder branch and then deleting the original refs over several runs, diluting the ratio with junk tags, two-ref repositories, and force-pushes to unrelated history. The history repository still preserves every deleted or rewritten ref in these cases.
- `GW_DELETE_GUARD=off` (or `0`) also disables the "every ref deleted" and "every branch deleted" protections; all guard rules are off.

GitWarden protects states it has actually collected. A change that appears and disappears between polling runs cannot be recovered. Git objects are in scope; LFS payloads and submodule repositories are not copied. The UI detects these references and marks the task as an incomplete backup so that the limitation is visible.

### When the safety gate holds a target

Before GitWarden changes an existing ref on a main mirror, it checks the change against the safety gate. A branch fast-forward passes directly. Any other change (a non-fast-forward branch update, a branch deletion, or any move or deletion of a tag) passes only when the overwritten object has been confirmed in that target's history repository during the same run: it must be the object of a confirmed preservation tag or, for branches, be contained in a confirmed preserved commit. While the history repository is healthy, a force push, branch deletion, or tag rewrite therefore passes automatically after its preservation tag is confirmed. The change is held when the preserved object cannot be confirmed there, for example because the history repository is unreachable or rejects the push; the target then reports **Blocked by safety gate** (`gate_blocked`). If the history repository already holds a `gw/*` tag with a different object, the target reports **History conflict** (`history_conflict`) instead, which takes precedence over `gate_blocked`. The main mirror is also held when any of its refs no longer matches the value GitWarden last pushed (**Mirror conflict**, `conflict`), which usually means another writer changed it.

A hold applies to the target's whole main mirror, not only to the affected ref: if any ref is blocked or in conflict, GitWarden pushes no ref updates or deletions to that main mirror in that run, including ordinary fast-forwards of other branches and tags. The source is still captured into the local archive, the old object stays preserved locally as `refs/preserved/<ULID>`, each run still tries to push pending preservation tags to the history repository, and other targets of the same task are processed independently. To recover, fix the cause: make the history repository reachable and writable for GitWarden, or stop the unexpected writer and return the conflicting ref to the value GitWarden last pushed. Then use **Retry** on the target card or wait for the next scheduled run. GitWarden confirms the pending `gw/*` tags first and then brings the main mirror up to date. The current version has no option that pushes the main mirror past the gate. See [Troubleshooting](docs/wiki/Troubleshooting.md#target-run-is-blocked-or-conflicted).

The history repository is not a second full mirror. It contains preserved versions, while the current refs remain in the main mirror and the local archive. Keep the main mirror or the local data volume when you need the latest collected state.

## Try the published image

For a quick trial without a checkout, run the published image with its own data volume, a loopback-only port, and the same log rotation as Compose, then open <http://127.0.0.1:8080>:

```bash
stty -echo; printf 'GitWarden admin password: '; IFS= read -r GW_ADMIN_PASSWORD; stty echo; echo
export GW_ADMIN_PASSWORD
docker run -d --name gitwarden-trial --restart unless-stopped -e GW_ADMIN_PASSWORD -p 127.0.0.1:8080:8080 -v gitwarden-trial-data:/data --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 ghcr.io/xjoker/gitwarden:20261016.1
unset GW_ADMIN_PASSWORD
```

The first line works in bash and zsh: `stty -echo` hides what you type, and `read` keeps the password out of your shell history. `-e GW_ADMIN_PASSWORD` without a value passes it from the environment, and `unset` removes it from your shell afterwards. If you run Docker through `sudo`, use `sudo --preserve-env=GW_ADMIN_PASSWORD docker run …`: plain `sudo` drops the variable and the container refuses to start. Avoid `sudo -E`, which hands your whole environment to root, including `DOCKER_HOST`, `DOCKER_CONTEXT` and proxy settings. GitWarden reads the password only from the `GW_ADMIN_PASSWORD` environment variable, so anyone who can access the Docker daemon can read it with `docker inspect`, whether it is passed with `-e` or `--env-file`. An environment file with mode 600 and `--env-file`, as shown in [Deployment](docs/wiki/Deployment.md#plain-docker), also keeps it out of shell history, but it is a plaintext file on disk. Do not publish the port on a public interface; see [Security](SECURITY.md).

The trial uses the volume `gitwarden-trial-data`, separate from Compose's `gitwarden-data`, but the same port 8080. Remove it with `docker rm -f gitwarden-trial` before starting Compose, and delete its data with `docker volume rm gitwarden-trial-data` once you no longer need it.

## Quick start with Docker Compose

Get a checkout of this repository and run the following from its project directory:

```bash
cp .env.example .env
```

Set the required administrator password in `.env`:

```dotenv
GW_ADMIN_PASSWORD=replace-this-before-start
```

Start the service and open <http://127.0.0.1:8080>:

```bash
docker compose up -d --build
docker compose logs -f gitwarden
```

The Compose configuration uses the project `.env`, the named volume `gitwarden-data`, and `127.0.0.1:8080`. The default image is built locally as `gitwarden:local`; Compose selects the host architecture automatically. The container runs as UID/GID `10001` and stores the application database, archives, credentials, and temporary work data under `/data`.

Release images are published to `ghcr.io/xjoker/gitwarden` after their `vYYYYMMDD.N` release tag passes CI. Each release pushes the version without the `v` (for example `20261016.1`) and `latest`. To run a published image, set `GW_IMAGE` and pull without rebuilding:

```bash
GW_IMAGE=ghcr.io/xjoker/gitwarden:20261016.1 docker compose pull
GW_IMAGE=ghcr.io/xjoker/gitwarden:20261016.1 docker compose up -d --no-build
```

See [Releases](https://github.com/xjoker/GitWarden/releases) for available versions and release notes.

For a non-Compose deployment, see [Deployment](docs/wiki/Deployment.md). For configuration, persistent-volume backups, and recovery, see the [Wiki source index](docs/wiki/Home.md).

## Create the first backup

1. Log in with `GW_ADMIN_PASSWORD`. The WebUI defaults to English and can switch to Simplified Chinese.
2. Open **Credentials** and save an HTTPS username/token or an SSH private key plus the matching `known_hosts` entry when the source or target is private. Leave the credential empty only for an anonymous endpoint. Never put credentials in a Git URL.
3. Choose **Back up a repository**. Step 1 records the source URL and sync interval. Step 2 either enters two manual target URLs or provisions a pair through a configured Gitea/Forgejo access. Step 3 requires confirming the target safety assumptions before creation.
4. Run the task once, then check the target card and **Logs**. The target card shows whether the main mirror is in sync, how many preservation records are confirmed in the history repository, and when the last audit ran.

For manual targets, both repositories must already exist and be empty when they are bound. Use a private main mirror and a private history repository. Give GitWarden the only write path to both repositories, and prevent CI or webhooks from reacting to backup pushes. A platform provisioned by GitWarden creates `<name>` and `<name>-history`, makes both private, disables repository features such as Actions, and protects `gw/*` on the history repository before binding them.

![Task detail and history status](docs/images/task-detail.jpg)

## Supported endpoints and platform extensions

The core accepts anonymous, HTTPS, SSH, SCP-style SSH, and local absolute-path Git endpoints. HTTPS credentials are passed through Git's askpass mechanism; SSH credentials use a temporary key file, strict host-key checking, and are removed after the operation.

A local absolute path is resolved inside the GitWarden process. In Docker, a host path that is not mounted into the container fails at sync time with `does not appear to be a git repository`. Mount the repository and enter the container path, for example `/repos/src.git`. Git also rejects a repository whose owner differs from the container user (`detected dubious ownership`), so the mounted repository must be owned by the container user (UID `10001` by default; for example `sudo chown 10001 /srv/git/src.git`, because Git checks only the top-level directory). A read-only mount is enough for a source. See [Deployment](docs/wiki/Deployment.md#local-source-repositories) for `docker run` and Compose examples and for repositories that are still pushed to on the host.

The optional platform integration currently supports **Gitea** and **Forgejo**. It can discover repositories, apply owner and pattern filters, create backup targets, harden them, and report repository/organization/system webhook metadata available to the configured token. A platform access requires an HTTPS credential with a username and token. The integration token is expected to have `read:admin`, `write:repository`, `write:organization`, and `write:user` as needed by the selected discovery and provisioning operations.

GitHub has no discovery or provisioning adapter. Add a GitHub source and its two target URLs manually in the three-step wizard, then use the same empty-target and single-writer rules.

## Operating model

The scheduler defaults to two workers, a five-minute minimum task interval, a fifteen-minute minimum discovery interval, and a ten-minute no-progress stall timeout. The interval can extend when recent runs are large and backs off after repeated failures. A run with Git progress may continue for as long as needed; only the no-progress threshold stops it.

The overview highlights incomplete tasks, target conflicts, gate blocks, source visibility changes, and overdue work. **Logs** contains run steps, ref changes, redacted Git output, and administrator activity. Run and activity records are retained for 90 days by default; preservation events, Git archives, and Git data are not removed by that cleanup.

Use `/health` for a read-only release check:

```bash
curl --fail http://127.0.0.1:8080/health
```

The response includes the application version and the build's Git commit when the image was built with one.

## Documentation

- [Wiki home](docs/wiki/Home.md)
- [Getting started](docs/wiki/Getting-started.md)
- [Deployment](docs/wiki/Deployment.md)
- [Configuration](docs/wiki/Configuration.md)
- [Backup and recovery](docs/wiki/Backup-and-recovery.md)
- [Platforms](docs/wiki/Platforms.md)
- [Troubleshooting](docs/wiki/Troubleshooting.md)
- [Release validation](docs/Validation.md)
- [Publishing (maintainers)](docs/Publishing.md)

The guides are in English. A Simplified Chinese overview is in [README.zh-CN.md](README.zh-CN.md).

## License

GitWarden is licensed under the [Apache License 2.0](LICENSE). Third-party dependencies retain their own licenses.
