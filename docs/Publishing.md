# Publishing

## Repository description

**Slogan:** Mirror the present. Preserve the past.

**About:** Self-hosted Git backups that preserve captured history before force pushes, rewritten tags, and deletions—with a bilingual WebUI.

Suggested GitHub topics:

```text
git backup git-backup git-mirror self-hosted disaster-recovery
version-control gitea forgejo python fastapi docker webui
```

## Before publishing a release

The public repository is [`xjoker/GitWarden`](https://github.com/xjoker/GitWarden), and release images are published to `ghcr.io/xjoker/gitwarden`.

1. GitWarden uses [Apache License 2.0](../LICENSE); keep the license in source and container distributions, and retain third-party license notices.
2. Verify the release branch includes only source, tests, example configuration, and public documentation. Review screenshots and commit author/committer metadata as well as file contents.
3. Run the checks in [Contributing](../CONTRIBUTING.md), then build and smoke-test the container. Confirm the GitHub Actions run for the release commit has passed.
4. Keep the About description, topics, and security reporting settings consistent with this page and [SECURITY.md](../SECURITY.md).
5. Push only the reviewed branch. Do not use `git push --all` or `--mirror` from a development repository containing private history.

## Versioned container releases

`VERSION` is the runtime release version, using `YYYYMMDD.N`. Check the current date before choosing a version and update [CHANGELOG.md](../CHANGELOG.md).

After the code has been published and CI passes, create a tag named `v` followed by the exact contents of `VERSION`. Publishing the tag starts the container release workflow. It checks the version and tests before publishing Linux amd64 and arm64 images to:

```text
ghcr.io/xjoker/gitwarden:<VERSION>
ghcr.io/xjoker/gitwarden:latest
```

The image name is `ghcr.io/` followed by the lowercased `owner/repository`. The workflow obtains the repository identity from GitHub and uses its scoped `GITHUB_TOKEN`; no personal access token belongs in the repository. Make the GHCR package public if anonymous pulls are intended. The image must actually be published before users can pull it.

Verify the registry manifest includes both architectures and compare the running `/health` response with the release version and commit. Prefer a version tag for deployments. Keep the data volume when replacing a container; see [Deployment](wiki/Deployment.md).

The build workflow follows Docker's documented [multi-platform GitHub Actions flow](https://docs.docker.com/build/ci/github-actions/multi-platform/).

## Wiki

[docs/wiki/Home.md](wiki/Home.md) is the maintained user-guide entry point. It is readable directly in the repository without enabling GitHub Wiki.

The GitHub Wiki is enabled but has no pages yet, so its Git repository cannot be cloned until a first page is created in the GitHub UI. To populate it, create that first page, clone the Wiki repository locally, and copy the Markdown files from `docs/wiki/` into that checkout. Include this page and [Validation](Validation.md) if they should appear in the Wiki; they live in `docs/`, not `docs/wiki/`. The guides are maintained in English only; any Chinese Wiki pages need their own review. Before committing, adjust repository-relative image links to point to the public repository's raw image URLs and adjust README/CONTRIBUTING links to their public repository URLs. Review the rendered pages, then publish the Wiki separately. Publishing a source-code branch does not publish the separate Wiki repository.
