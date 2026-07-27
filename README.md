# Pullbox Direct Download Providers

Optional, separately deployed direct-download discovery providers for
[Pullbox](https://github.com/pullboxapp/pullbox).

## Status

This repository is being prepared for Pullbox v1.1.0. Provider contracts and
implementations are not yet available for production use.

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

## License

Pullbox Direct Download Providers is licensed under GPL-3.0-or-later. See
`LICENSE` for details.
