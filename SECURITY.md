# Security Policy

## Reporting Vulnerabilities

Please report suspected vulnerabilities privately through GitHub private
vulnerability reporting when available. If that is not possible, email
`security@pullbox.app` with reproduction steps and the affected commit or image
version. Do not include live credentials, signed download URLs, account data,
or copyrighted archives in a report unless the maintainers explicitly request
a secure transfer.

## Provider Boundary

Direct-download providers are untrusted discovery services. A supported
deployment must keep each provider on a private network and must not grant it:

- Pullbox data, config, import, download, quarantine, or library mounts.
- Database access, Pullbox sessions, or artifact-host account credentials.
- The Docker socket, privileged mode, added Linux capabilities, or persistent
  queues.
- A public host port unless an operator deliberately accepts that risk.

Providers require a unique bearer token containing at least 32 characters.
Pullbox remains responsible for semantic matching, artifact planning,
downloads, archive validation, post-processing, history, and library state.

The synthetic provider and fixtures contain generated test data only. Never
commit source-account credentials, signed URLs, cookies, personal information,
or downloaded publications to this repository.

## Automated Security Gates

Pull requests run secret scanning, strict Python dependency auditing, Bandit,
dependency review, CodeQL extended security queries, workflow linting, runtime
smoke tests, multi-architecture builds, and Grype image scans. Public and fork
pull requests run on GitHub-hosted runners with read-only default permissions;
pull request workflows do not publish images or receive registry credentials.

The pinned Python base image currently has reviewed upstream findings that are
tracked in `.github/security/container-vulnerability-baseline.json`. The
baseline is not a suppression of scanner output: new High or Critical findings
fail CI, full reports remain available, and every accepted entry requires a
rationale and expiry date. Base image and dependency updates should remove
entries as fixes become available.
