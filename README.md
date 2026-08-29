# Pullbox Direct Download Providers

Optional, separately deployed direct-download discovery providers for
[Pullbox](https://github.com/pullboxapp/pullbox).

## Status

This repository provides the version-one Pullbox direct-download protocol,
Python DTO package, compatibility policy, conformance runner, synthetic
reference provider, the official GetComics and Anna's Archive providers, and a
release-gated LibGen community provider. Published production images are
independently versioned, multi-architecture, scanned, signed, and available
from GHCR and Docker Hub.

## Source Providers

- GetComics: metadata discovery and stateless artifact-route normalization.
- Anna's Archive: metadata discovery with opt-in member fast-download
  resolution; a member secret is required only by the resolve operation.
- LibGen: bounded HTML discovery, keyed metadata enrichment, and same-source
  generic HTTPS resolution with an editable validated source origin. Its image
  remains independently gated until the LibGen release decision is approved.

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
  libgen/
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
make security-check
make docker-conformance
make docker-source-smoke
```

`make validate` runs Ruff, strict mypy, unit/conformance tests, and the 90%
coverage gate. `make docker-conformance` builds the digest-pinned synthetic
image and proves the protocol over an internal-only Docker network.
`make security-check` runs Bandit and a strict dependency audit.

The source-provider Compose harness builds all source providers, waits for
process-only socket healthchecks, and validates authenticated manifests over an
internal-only network. Process healthchecks intentionally do not call upstream
sources. The harness uses generated test credentials and performs no live
source search or payload download. Live acceptance probes must remain
metadata-only, use credentials supplied at runtime, and never persist signed
URLs or account data.

Pull requests run four stable aggregate checks: `CI Required`,
`Security Required`, `Workflow Hygiene Required`, and
`Container Security Required`. They run on GitHub-hosted runners with read-only
default permissions. The security gate includes Gitleaks, strict Python
dependency auditing, Bandit, dependency review, and CodeQL's extended security
queries scoped to shipped provider code. Container checks build and smoke-test
all four runtime images, scan them with Grype, and prove Linux AMD64 and ARM64
builds without publishing.

High and Critical findings inherited from the pinned public Python base image
are recorded in an expiring reviewed baseline under
`.github/security/container-vulnerability-baseline.json`. New findings fail the
build, removed findings disappear automatically, and the baseline must be
reviewed before its expiry date. Complete scanner reports are retained as CI
artifacts. Trusted runs upload only unreviewed High or Critical findings to
GitHub Security, so the code-scanning dashboard stays actionable while the
expiring baseline remains the audit trail for accepted upstream risk.

Provider-prefixed semantic-version tags publish one signed provider image to
both registries. The pipeline validates the candidate before creating runnable
tags, publishes identical Linux AMD64/ARM64 manifests, retains SBOM and
provenance attestations, signs both registry digests with keyless Cosign, and
creates a provider-specific GitHub Release only after verification succeeds.
The synthetic image remains a protocol test tool, not a comic discovery source.

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

### Native Provider Configuration

Provider settings use a closed, documented vocabulary:

- `enum` declares closed choices. The configured value must be one of the
  declared values.
- `x-pullbox-suggestions` declares editable HTTPS origin suggestions. A user may
  enter another safe public HTTPS origin because the list is not an allowlist.
- `x-pullbox-source-origin` marks a field as the provider's effective source
  origin for provider-scoped link and browser-resolver policy.

Suggestions and source-origin marking are independent controls. A provider may
offer editable suggestions without changing its source origin, or allow a custom
source origin without supplying suggestions. Pullbox validates suggested and
default origins before rendering or saving them and validates the selected
origin again before use.

Search candidates may include an optional content fingerprint formatted as
`md5:<32 lowercase hexadecimal characters>`. It must remain stable only while
the candidate bytes are identical; changed bytes require a new fingerprint.
Pullbox uses it only for deduplication and fallback grouping, never as a security
or authenticity checksum, and does not treat it as durable library metadata.

## Source Provider Behavior

### GetComics

The GetComics provider requires no source-account credential. It searches the
declared `getcomics.org` domain, resolves release pages into normalized artifact
groups and mirrors, and fails closed when a layout cannot be parsed safely. It
tries ordinary HTTP first and uses an operator-configured browser resolver only
after a recognized challenge. It never downloads or proxies artifact bytes.

### Anna's Archive

The Anna's Archive provider is an explicit opt-in integration. Its configurable
official URL accepts only `https://annas-archive.gl`,
`https://annas-archive.pk`, or `https://annas-archive.gd`. Pullbox renders the
field as a closed selector with those exact choices; lookalike and arbitrary
domains remain rejected. Unattended resolution requires the user's
member fast-download secret; free slow-download automation, CAPTCHA bypass,
unofficial domains, and payload proxying are not supported.

Pullbox stores the member secret encrypted and sends it only in the active
`POST /v1/resolve` request. The provider keeps no database or cache and must not
log the credential, account metadata, or returned signed URL. Authentication,
quota, source availability, and malformed responses remain distinct failures.
Opening search-result details must not call resolve because a fast-link request
may consume source quota.

Search attempts the selected official Anna's Archive page first. When that page
is blocked by a browser challenge, is temporarily unavailable, or returns no
candidates, the provider performs a bounded fallback against the LibGen comics
catalog. Only candidates with a matching lowercase LibGen ID and MD5 content
fingerprint are considered for Anna's Archive discovery, with canonical files
listed before mobile derivatives. Catalog presence does not guarantee that Anna
offers a member fast-download route, so availability is verified only when the
user grabs the result. Catalog-derived candidates intentionally do not expose a
cross-provider fingerprint to Pullbox: if Anna cannot resolve the record,
Pullbox reports that failure instead of silently downloading it from LibGen.
Resolution still uses the official member fast-download JSON API, and the member
secret is never sent to LibGen. This fallback covers only Anna's Archive records
sourced from LibGen and does not claim parity with Anna's Archive's complete
catalog. The Anna's Archive image includes the catalog-discovery dependency and
does not require a separate LibGen provider container.

Successful resolves may report provider-generic remaining/limit/window quota
telemetry. The response intentionally excludes account identity and download
history. Pullbox stores only the latest capacity observation, applies its
operator-configured automatic reserve, and may continue to another already
accepted source when Anna's Archive is unavailable. Manual grabs may use the
reserved slots. Quota errors may include a bounded `retry_after_seconds` hint
so Pullbox can recover automatically even without an earlier capacity report.

### LibGen

The LibGen provider is a separately packaged community integration. It accepts
the documented LibGen origins as editable suggestions, validates the selected
public HTTPS origin before every operation, and attempts ordinary HTTP before
using a request-scoped browser resolver for a recognized source gate. Search is
bounded to three query variants and keyed metadata enrichment; positive and
negative caches are process-local and bounded.

Candidate and artifact identity are revalidated by lowercase MD5, keyed file
metadata, and edition relationships before a same-source public HTTPS artifact
is returned. The MD5 is content identity and deduplication evidence, not a
security guarantee. The provider does not proxy payload bytes, retain resolver
cookies, expose full artifact URLs in logs, or reuse Anna's Archive links.
Known-source failover is bounded to one alternate origin per operation.

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

## Production Images

Official images are available from either registry. Use the same version for
either registry; both names resolve to the same signed digest.

| Provider | GHCR | Docker Hub |
| --- | --- | --- |
| GetComics | `ghcr.io/pullboxapp/pullbox-provider-getcomics:1.0.2` | `docker.io/pullbox/pullbox-provider-getcomics:1.0.2` |
| Anna's Archive | `ghcr.io/pullboxapp/pullbox-provider-annas-archive:1.0.1` | `docker.io/pullbox/pullbox-provider-annas-archive:1.0.1` |

LibGen image publication is intentionally omitted from this table until its
independent release gate is approved and a numbered provider release exists.

Pin a numbered version or the immutable digest in production. `latest` tracks
only the newest stable provider release; prerelease and manual `edge` builds do
not move it. Each provider has an independent lifecycle, so their version
numbers may diverge after the initial release.

Generate a different bearer token for each provider and keep the services on a
private Docker network with Pullbox:

```bash
openssl rand -hex 32
```

The containers require only `PULLBOX_PROVIDER_TOKEN`, expose port `8780` to the
private network, run as UID/GID `65532:65532`, and need no host volumes. Keep a
read-only root filesystem, drop all capabilities, enable `no-new-privileges`,
and provide only a bounded `/tmp` tmpfs, as shown in the Pullbox deployment
documentation.

Release maintainers should follow [`docs/RELEASING.md`](docs/RELEASING.md).

## Security

Do not open public issues for suspected vulnerabilities. Follow
[`SECURITY.md`](SECURITY.md) for private reporting. Provider bearer tokens must
contain at least 32 characters and must be unique per deployment.

## License

Pullbox Direct Download Providers is licensed under GPL-3.0-or-later. See
`LICENSE` for details.
