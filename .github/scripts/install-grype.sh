#!/usr/bin/env bash
# Pinned scanner for Linux AMD64 GitHub-hosted runners (also scans ARM64 images).
set -euo pipefail
if [ "$(uname -s)/$(uname -m)" != "Linux/x86_64" ]; then
  echo "This installer requires a Linux AMD64 runner." >&2
  exit 1
fi
grype_dir=$(mktemp -d "${RUNNER_TEMP:?}/grype.XXXXXX")
archive="${grype_dir}/grype.tar.gz"
curl --fail --silent --show-error --location --retry 3 \
  https://github.com/anchore/grype/releases/download/v0.119.0/grype_0.119.0_linux_amd64.tar.gz \
  --output "${archive}"
printf '%s  %s\n' 3fa2dc4b924621ab65404cf08d0b8438d896d80ab949c9d5a4ca283c36004c9b "${archive}" | sha256sum --check -
tar -xzf "${archive}" -C "${grype_dir}" grype
printf '%s\n' "${grype_dir}" >> "${GITHUB_PATH:?}"
