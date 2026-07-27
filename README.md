# Pullbox Direct Download Providers

Optional, separately deployed direct-download discovery providers for
[Pullbox](https://github.com/pullboxapp/pullbox).

## Status

This repository is being prepared for Pullbox v1.1.0. The version-one protocol,
Python DTO package, and synthetic conformance provider are candidate contracts.
GetComics and Anna's Archive implementations are not yet available for
production use.

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

The planned repository structure is:

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

The local Docker harness intentionally publishes no host port. The provider
runs as UID/GID `65532:65532` with a read-only root filesystem, a bounded
`tmpfs`, all Linux capabilities dropped, and `no-new-privileges`. It receives no
host volume, Pullbox path, database, Docker socket, or artifact-host account
credential.

## Protocol

The canonical candidate is
[`spec/direct-download-provider-v1.openapi.yaml`](spec/direct-download-provider-v1.openapi.yaml).
Sanitized protocol fixtures live under `tests/fixtures/protocol-v1`. Compatible
implementations must expose exactly four bearer-authenticated operations:

- `GET /v1/manifest`
- `GET /v1/health`
- `POST /v1/search`
- `POST /v1/resolve`

The protocol is still a release candidate. Incompatible changes are permitted
until DD-0 is complete; after publication, breaking changes require a new
protocol major version.

## Security

Do not open public issues for suspected vulnerabilities. Follow
[`SECURITY.md`](SECURITY.md) for private reporting. Provider bearer tokens must
contain at least 32 characters and must be unique per deployment.

## License

Pullbox Direct Download Providers is licensed under GPL-3.0-or-later. See
`LICENSE` for details.
