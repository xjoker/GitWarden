# Security

GitWarden is a single-administrator service intended for trusted networks. Keep the default loopback binding, or place it behind an authenticated HTTPS access layer. Do not expose the service directly to the public internet.

Credentials are stored in the local SQLite database with restrictive file permissions; they are **not encrypted at rest**. Protect the data volume and its backups. Treat source code archives and recovery bundles as confidential when the source repository is private.

SSH connections require a supplied `known_hosts` entry and strict host-key verification. Give source credentials read access and target credentials only the access needed to write the designated backup repositories. Platform-wide discovery can require broader permissions; see [Platforms](docs/wiki/Platforms.md).

Only GitWarden should write to its target repositories. Target repositories must be private, with CI and webhooks disabled or otherwise prevented from executing mirrored code. Protect the `gw/*` tags in the history repository from other writers.

Login sessions are held in memory and are cleared when the service restarts. Stop the service before taking a consistent backup of the complete data volume.

## Reporting a vulnerability

Report vulnerabilities privately through the repository's **Security → Report a vulnerability** feature: <https://github.com/xjoker/GitWarden/security/advisories/new>. Do not put credentials, exploit details involving a live service, or private repository contents in a public issue. If you cannot use private reporting, open a **Bug report** issue titled "Security contact request", write only "security contact request" in the required text fields, choose **Other** for **Deployment**, tick the two required checkboxes under **Checks**, and wait for a maintainer to provide a private channel. Do not include any details of the vulnerability in that issue.
