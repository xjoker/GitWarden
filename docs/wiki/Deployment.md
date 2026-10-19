# Deployment

GitWarden is distributed as a single container that runs the WebUI and scheduler in one process. The supported deployment shape is Docker Compose with a persistent named volume. The container listens on port `8080`, runs as UID/GID `10001`, and uses UTC.

## Compose deployment

Run these commands from the project directory:

```bash
cp .env.example .env
chmod 600 .env
```

Set at least `GW_ADMIN_PASSWORD` in `.env`, then validate the rendered Compose file and start the local build:

```bash
docker compose config --quiet
docker compose up -d --build
curl --fail http://127.0.0.1:8080/health
```

Compose reads the project `.env` for interpolation and container environment. The application itself does not search for or load `.env`; when running outside Docker, export environment variables or provide them through the process manager. The default Compose setup uses:

- image `gitwarden:local`, built from the project directory;
- named volume `gitwarden-data` mounted at `/data`;
- host binding `127.0.0.1:8080:8080`;
- automatic host architecture selection for amd64 or arm64.

The default binding is local-only. To allow access from a controlled network interface, set `GW_BIND` in `.env` to the exact host address you intend to expose and apply the corresponding firewall policy. `GW_PORT` defaults to 8080 and changes both the application port and the Compose port mapping. For plain Docker, update `--publish` to match when changing this value.

Open <http://127.0.0.1:8080>, sign in, and complete [Getting started](Getting-started.md).

## Run a published image

Release images are made available after a `vYYYYMMDD.N` tag, matching `VERSION`, passes CI. The release build publishes Linux amd64 and arm64 images to `ghcr.io/xjoker/gitwarden`; it does not require a manually created PAT.

Pull and start a published version without a local build:

```bash
GW_IMAGE=ghcr.io/xjoker/gitwarden:20261016.1 docker compose pull
GW_IMAGE=ghcr.io/xjoker/gitwarden:20261016.1 docker compose up -d --no-build
curl --fail http://127.0.0.1:8080/health
```

Choose an existing version from [Releases](https://github.com/xjoker/GitWarden/releases). Do not mix a local build and a release image in the same verification step; check `/health` after the container is recreated.

## Plain Docker

Use a named volume and the same environment file, a plaintext file on disk, when Compose is not desired:

```bash
docker build --build-arg GIT_COMMIT="$(git rev-parse HEAD)" -t gitwarden:local .
docker volume create gitwarden-data
docker run -d \
  --name gitwarden \
  --restart unless-stopped \
  --env-file .env \
  --publish 127.0.0.1:8080:8080 \
  --volume gitwarden-data:/data \
  gitwarden:local
curl --fail http://127.0.0.1:8080/health
```

`docker volume create` is a one-time command. The container must retain write access to `/data`; the image's application user is UID/GID `10001`. If a bind mount is used instead of the named volume, create the directory first and assign it to `10001:10001`.

## Local source repositories

A source URL that starts with `/` is read inside the container, not on the host. Mount the host repository and enter the container path in the wizard. Without the mount, the sync fails with `fatal: '/srv/git/src.git' does not appear to be a git repository`.

Git refuses a repository whose top-level directory is not owned by the user running it. With the default image user, a host repository owned by another UID fails with `fatal: detected dubious ownership in repository at '/repos/src.git'`. There is no supported way to add a `safe.directory` exception from outside the application: GitWarden runs Git with `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null` and its own `HOME`, so neither `/etc/gitconfig` (including one baked into a custom image) nor `~/.gitconfig` is read, and Git itself drops `-c` and `GIT_CONFIG_COUNT` settings when it starts `upload-pack` for a local path. Make the repository owned by the container user instead (UID `10001` by default).

Git checks only the owner of the top-level directory, so changing that directory is enough for GitWarden to read the repository, provided its contents are readable by other users (the default with umask `022`). A repository created under umask `077` still fails after a top-level-only change, with the same `does not appear to be a git repository` message as a missing mount; use `chown -R` for such a repository. Both `chown -R` and the top-level-only change restrict host users that push to the repository (see below):

```bash
sudo chown 10001 /srv/git/src.git        # top-level directory only; contents must be readable by others
docker run -d \
  --name gitwarden \
  --restart unless-stopped \
  --env-file .env \
  --publish 127.0.0.1:8080:8080 \
  --volume gitwarden-data:/data \
  --volume /srv/git/src.git:/repos/src.git:ro \
  gitwarden:local
```

With Compose, add the mount to the service's existing `volumes` list:

```yaml
    volumes:
      - gitwarden-data:/data
      - /srv/git/src.git:/repos/src.git:ro
```

Then use `/repos/src.git` as the source URL. A read-only mount is enough because a source is only fetched.

### Repositories that are still pushed to on the host

After the ownership change, a host user that pushes to the repository over a local path first gets `detected dubious ownership`. After `git config --global --add safe.directory /srv/git/src.git`, a repository changed with `chown -R 10001:10001` then rejects the push with `unable to create temporary object directory`. A top-level-only `chown 10001` without group write access accepts updates to most branches, but rejects a push to the branch `HEAD` points to with `Unable to create '/srv/git/src.git/./HEAD.lock': Permission denied` and the deletion of a packed branch with `Unable to create '/srv/git/src.git/./packed-refs.lock': Permission denied`.

Each of the following was tested with Git 2.47.3: host users pushing over a local path, and GitWarden syncing the read-only mount afterwards. `<group>` is a group that the pushing users belong to; in the tests it was a supplementary group of two users with different primary groups.

- **Shared group (one or more pushing users).** Make the repository group-writable with setgid directories, and give each pushing user a `safe.directory` entry:

  ```bash
  git --git-dir=/srv/git/src.git config core.sharedRepository group
  sudo chown -R 10001:<group> /srv/git/src.git
  sudo chmod -R g+rwX /srv/git/src.git
  sudo find /srv/git/src.git -type d -exec chmod g+s {} +
  git config --global --add safe.directory /srv/git/src.git   # as each pushing user
  ```

  With two users pushing in turn, pushes to the `HEAD` branch, new branches, deletions of packed branches, force pushes and `git gc` all succeeded, and GitWarden synced every change. The same commands work after `git init --bare --shared=group` for a new repository. That command also sets `receive.denyNonFastforwards=true`, which rejects force pushes; the commands above do not, so add `git config receive.denyNonFastforwards true` if you want that. The container user is not in `<group>` and reads through the permissions for other users. If a pushing user has umask `077`, new objects are not readable by others and the next sync fails (in the test with `upload-pack: not our ref`); in that case use `core.sharedRepository 0664` and `chmod -R g+rwX,o+rX`.

- **Top-level directory with group write (single pushing user).** Change only the top-level directory and make it group-writable; the contents keep their current owner. This works only when every push comes from the user that owns the repository contents. Another member of `<group>` gets `unable to create temporary object directory`; use the shared-group option for several pushing users:

  ```bash
  sudo chown 10001:<group> /srv/git/src.git
  sudo chmod g+ws /srv/git/src.git
  git config --global --add safe.directory /srv/git/src.git   # as each pushing user
  ```

- **Mirror owned by the container user.** Leave the original repository unchanged and mount a mirror instead. The refresh runs as UID `10001` and also hits the ownership check when it reads the original. `git -c safe.directory=...` has no effect there; the entry must be in that user's global configuration:

  ```bash
  # as UID 10001, with HOME pointing to a directory owned by 10001
  git config --global --add safe.directory /srv/git/src.git
  git clone --mirror /srv/git/src.git /srv/git/mirrors/src.git
  git --git-dir=/srv/git/mirrors/src.git fetch --prune origin   # run periodically
  ```

  Mount `/srv/git/mirrors/src.git` instead. GitWarden sees new commits only after the next refresh.

Serving the repository over SSH or HTTPS and using that URL as the source avoids the ownership check entirely. That setup was not tested here.

### Running the container as the repository owner

Alternatively, leave the repository unchanged and run the container with `--user <uid>:<gid>` of the repository owner. `/data` must then be writable by that UID. With the default named volume, which is owned by `10001`, the container exits at startup with `PermissionError: [Errno 13] Permission denied: '/data/gitwarden.db'`. Either mount `/data` from a host directory owned by the same UID (`--volume /srv/gitwarden-data:/data`), or stop the container and change the owner of the existing data first:

```bash
docker run --rm --user 0 --volume gitwarden-data:/data --entrypoint chown gitwarden:local -R <uid>:<gid> /data
```

That `chown` does not persist on a new, empty named volume: Docker copies the image's `/data` ownership into an empty volume when it is next mounted. Use a bind mount for a new deployment, or add `:nocopy` to the volume (`gitwarden-data:/data:nocopy`) in both the `chown` command and the first container run. Once the application has written its data, the volume is no longer empty and later runs keep the ownership without `:nocopy`.

## Persistence and lifecycle

The named volume contains the SQLite database, local bare archives, stored credentials, and temporary work data. Keep `.env` separately because it contains the administrator password. `docker compose down` leaves the named volume in place. `docker compose down -v` removes volumes and therefore can destroy the only local copy of the collected history; use it only when that data has been backed up and removal is intentional.

To stop the service for maintenance while retaining data:

```bash
docker compose stop
docker compose start
```

To update a locally built deployment, rebuild and recreate it:

```bash
docker compose up -d --build
curl --fail http://127.0.0.1:8080/health
```

To update a release-image deployment, change the `GW_IMAGE` tag, pull, recreate with `--no-build`, and check `/health`. Keep the previous tag available for a deliberate rollback. A successful `docker compose pull` only proves that the image was downloaded; the health response is the deployment check.

## Release image contract

Release tags use `vYYYYMMDD.N`, where the date and increment match the root `VERSION` file. CI builds and pushes amd64/arm64 images only after the tests pass for that tag. The image build receives the source commit as `GW_GIT_COMMIT`, which is exposed by `/health` for post-deploy verification.
