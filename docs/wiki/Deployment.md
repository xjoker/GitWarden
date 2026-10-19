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

Use a named volume and the same environment file when Compose is not desired:

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

Git refuses a repository that is not owned by the user running it. With the default image user, a host repository owned by another UID fails with `fatal: detected dubious ownership in repository at '/repos/src.git'`. GitWarden ignores global and system Git configuration, so `safe.directory` cannot be set from outside the application; make the repository owned by UID `10001` instead:

```bash
sudo chown -R 10001:10001 /srv/git/src.git
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

Then use `/repos/src.git` as the source URL. A read-only mount is enough because a source is only fetched. After the ownership change, anything that pushes to the host repository must run as UID `10001` or keep that ownership. Alternatively, run the container with `--user` set to the repository owner's UID:GID and mount `/data` from a host directory owned by the same UID, so the application can still write its data.

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
