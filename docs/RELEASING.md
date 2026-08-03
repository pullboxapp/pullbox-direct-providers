# Provider Image Release Process

The GetComics, Anna's Archive, and synthetic providers share one production
pipeline but retain independent version histories. A provider release publishes
one image to GHCR and Docker Hub, proves that both registries expose the same
multi-architecture digest, signs both copies, and then creates one GitHub
Release.

## Required Repository Configuration

The repository must have these Actions secrets:

- `DOCKERHUB_USERNAME`: the Docker Hub namespace owner with repository write
  access.
- `DOCKERHUB_TOKEN`: a Docker Hub personal access token with read/write access.

Use a dedicated, expiring token for this repository and rotate both the Docker
Hub credential and GitHub Actions secret before it expires. Delete permission
is not required by the release workflow.

GitHub's `GITHUB_TOKEN` provides GHCR publication and GitHub Release access.
Release jobs use GitHub-hosted runners, read-only default permissions, and
job-scoped write permissions. Keyless Cosign signing uses the job's OIDC token;
there is no stored signing key.

The protected tag ruleset must cover:

- `getcomics-v*`
- `annas-archive-v*`
- `synthetic-v*`

Pre-create the public `pullbox-provider-getcomics` and
`pullbox-provider-annas-archive` Docker Hub repositories with their expected
descriptions and overviews. Configure numbered stable and canonical Python
prerelease versions plus `sha-*` tags as immutable while allowing `latest`,
internal `candidate-*`, and manual `edge` rehearsal tags to advance. Docker Hub
Scout coverage is optional; the blocking release security gate is the reviewed
Grype scan that runs before runnable tags are created.

## Release Preparation

1. Land changes through a pull request to `develop`.
2. Require `CI Required`, `Security Required`, `Workflow Hygiene Required`, and
   `Container Security Required` to pass.
3. Promote `develop` to `main` through a reviewed pull request and verify the
   same required checks.
4. Confirm the exact provider and version before creating a tag. Never infer or
   silently increment a release version.
5. Set the selected provider package version to the exact release version:
   `providers/getcomics/pyproject.toml` for GetComics,
   `providers/annas_archive/pyproject.toml` for Anna's Archive, or the root
   `pyproject.toml` for the synthetic conformance image.
6. Confirm the release commit is the intended `main` commit and the worktree is
   clean. The workflow independently rejects any release tag whose commit is
   not already merged into `main`.

Stable provider tags use strict semantic versions:

```text
getcomics-v1.0.0
annas-archive-v1.0.0
synthetic-v1.0.0
```

Prereleases use the canonical Python package form exposed by the running
provider, for example `getcomics-v1.1.0rc1`. Hyphenated forms such as
`getcomics-v1.1.0-rc1` and abbreviated forms such as `v1.0` are not accepted.
This keeps the Git tag, image tag, OCI version, package metadata, and provider
manifest version identical.

## Create A Release

Create a signed annotated tag on the approved `main` commit, then push only that
tag:

```bash
git switch main
git pull --ff-only origin main
git tag -s getcomics-v1.0.0 -m "GetComics provider v1.0.0"
git push origin getcomics-v1.0.0
```

Repeat with the Anna's Archive prefix when releasing that provider. Releasing
both providers at the same version still uses two tags and two independent
workflow runs.

The `Provider Image Release` workflow then:

1. Validates the tag and maps it to an allowlisted Dockerfile and image names.
2. Builds Linux AMD64 and ARM64 images and pushes untagged platform blobs by
   immutable digest to GHCR and Docker Hub.
3. Scans the exact AMD64 candidate against the reviewed Grype baseline.
4. Runs the candidate with a read-only root, no capabilities, and
   `no-new-privileges`, then verifies authenticated manifest/health responses
   and rejection of unauthenticated requests.
5. Creates an internal `candidate-*` multi-platform manifest in each registry
   and confirms both registries expose the same digest, both runnable platforms,
   OCI descriptions, SBOM, and provenance.
6. Signs and verifies both candidate registry digests with Cosign.
7. Promotes the verified digest to runnable version, SHA, manual `edge`, and
   eligible stable `latest` tags, then verifies every tag resolves to the signed
   digest.
8. Uploads trusted release metadata for the downstream `Release` workflow.

The downstream workflow revalidates tag ownership and both signatures before
creating the provider-specific GitHub Release. Manual dispatches publish an
`edge` image for release rehearsal but never create a GitHub Release or move
`latest`.

## Post-Release Verification

For the released provider and version:

1. Confirm both workflows are green.
2. Confirm the GitHub Release exists on the expected provider tag.
3. Confirm GHCR and Docker Hub expose `linux/amd64` and `linux/arm64` plus the
   expected attestation manifests.
4. Confirm both registries report the exact digest recorded in the GitHub
   Release.
5. Run both Cosign verification commands from the GitHub Release notes.
6. Pull and smoke-test the numbered tag from each registry.
7. Confirm the package/repository is public and linked back to this source
   repository.

Do not describe an image as signed or released until all of these checks pass.

## Failure And Rollback

A failure before signed-digest promotion leaves no runnable version, SHA, or
`latest` tag. The internal `candidate-*` reference may remain for diagnosis but
is not a supported user pull target. A failure after signed promotion but before
GitHub Release creation leaves a signed, verified image without its release
page; investigate and rerun the downstream release workflow without moving or
recreating the immutable numbered tag.

Provider rollback is independent from Pullbox. Pin the prior numbered provider
tag or digest, recreate only that provider container, verify health and protocol
compatibility, and leave Pullbox data untouched. `latest` is a convenience tag,
not a rollback target.
