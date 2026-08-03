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
fail CI, full reports remain available as CI artifacts, and every accepted
entry requires a rationale and expiry date. Only unreviewed High or Critical
findings are uploaded to the actionable GitHub code-scanning dashboard. Base
image and dependency updates should remove entries as fixes become available.

## Release Integrity

Only provider-prefixed semantic-version tags can publish production images.
The release workflow builds Linux AMD64 and ARM64 images by immutable digest,
checks the reviewed vulnerability baseline, smoke-tests the hardened candidate,
and only then creates runnable tags in GHCR and Docker Hub. Both registries must
resolve to the same digest before the image is signed.

Every release image includes SBOM and provenance attestations and is signed in
both registries with keyless Sigstore/Cosign through GitHub Actions OIDC. The
GitHub Release is created only after both signatures are verified. Operators
should pin a numbered version or digest and use the verification commands in
the corresponding GitHub Release notes.
