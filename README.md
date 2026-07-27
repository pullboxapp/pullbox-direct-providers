# Pullbox Direct Download Providers

Optional, separately deployed direct-download discovery providers for
[Pullbox](https://github.com/pullboxapp/pullbox).

## Status

This repository is being prepared for Pullbox v1.1.0. The version-one protocol,
Python DTO package, compatibility policy, conformance runner, and synthetic
reference provider are frozen for DD-2. GetComics and Anna's Archive
implementations are not yet available for production use; they are separate
DD-6 and DD-7 deliverables.

## Planned Providers

- GetComics
- Anna's Archive

Each provider will run as an independent, stateless OCI service and implement a
versioned, language-neutral Pullbox provider contract.

## Boundary

This repository is not a general Pullbox plugin platform. Providers will own
source-specific discovery and normalization only. Pullbox remains responsible
for matching, artifact selection, credentials, downloads, validation,
post-processing, history, and library state.

The repository structure is:

```text
spec/
  direct-download-provider-v1.openapi.yaml
packages/
  provider_contract/
providers/
  synthetic/
  getcomics/
  annas_archive/
tests/
  conformance/
  fixtures/
docker/
```

Implementation will follow contract-first TDD. The synthetic provider and
conformance suite must pass before either source provider is implemented.

## Development

Python 3.12 through 3.14 are supported by the contract package. Official
provider containers run Python 3.14.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
make validate
make docker-conformance
```

`make validate` runs Ruff, strict mypy, unit/conformance tests, and the 90%
coverage gate. `make docker-conformance` builds the digest-pinned synthetic
image and proves the protocol over an internal-only Docker network.

CI also builds the synthetic reference image for Linux AMD64 and ARM64 without
publishing it. Tags matching `synthetic-v*` can publish a signed, multi-arch
reference image to GHCR with SBOM and provenance attestations. That image is a
protocol test tool, not a comic discovery source and not a production provider.

The local Docker harness intentionally publishes no host port. The provider
runs as UID/GID `65532:65532` with a read-only root filesystem, a bounded
`tmpfs`, all Linux capabilities dropped, and `no-new-privileges`. It receives no
host volume, Pullbox path, database, Docker socket, or artifact-host account
credential.

## Protocol

The canonical version-one contract is
[`spec/direct-download-provider-v1.openapi.yaml`](spec/direct-download-provider-v1.openapi.yaml).
Sanitized protocol fixtures live under `tests/fixtures/protocol-v1`. Compatible
implementations must expose exactly four bearer-authenticated operations:

- `GET /v1/manifest`
- `GET /v1/health`
- `POST /v1/search`
- `POST /v1/resolve`

Pullbox negotiates the intersection of the exact protocol versions declared by
both sides. Breaking changes require a new protocol major version; a provider
must never guess compatibility with an undeclared version.

Provider configuration is data, not executable UI. A manifest may declare only
the allowlisted native control types in the contract. Pullbox validates those
controls, renders its own settings UI, and rejects provider-supplied HTML,
JavaScript, or unknown configuration fields.

## Deployment And Registration

Provider services are deployed separately from Pullbox. An operator creates a
unique bearer token of at least 32 characters, starts the provider on a private
container network or an HTTPS endpoint, and then registers that endpoint and
token under **Settings > Direct Downloads** in Pullbox.

Pullbox validates the endpoint, reads the manifest, negotiates compatibility,
tests health, and stores the token encrypted. New registrations remain disabled
until the operator explicitly enables them. Custom provider identities require
an additional trust acknowledgement. Remote endpoints require HTTPS; private
HTTP is available only through an explicit warning and private-address policy.

Pullbox does not pull, start, update, or remove provider containers and never
mounts the Docker socket. Image deployment, network isolation, updates, and
rollback remain operator responsibilities. Disabling or removing a registration
stops future use without granting the provider access to Pullbox paths, its
database, download-client credentials, or artifact-host credentials.

## Security

Do not open public issues for suspected vulnerabilities. Follow
[`SECURITY.md`](SECURITY.md) for private reporting. Provider bearer tokens must
contain at least 32 characters and must be unique per deployment.

## License

Pullbox Direct Download Providers is licensed under GPL-3.0-or-later. See
`LICENSE` for details.
