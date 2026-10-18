# Byparr Wait Correction

Private deployment overlay for Byparr 3.0.4 (`cb2a862386e92f141e8aa3b58f8532ef2fc36ed0`).
The base digest is the existing deployed image; no dependencies are changed.
This is not a new upstream Byparr release or a public provider release.

The patch waits for a recognized DDoS-Guard interstitial to finish, including
the intermediate Loading page, within the original request timeout. It then
captures fresh HTML after settling. Cloudflare retains its existing solver.
Cancelled or exhausted waits must not return challenge HTML as successful results.
Source-domain validation and challenge rejection remain in the provider.

Build from the provider repository root:

```sh
docker build -f docker/byparr/Dockerfile --build-arg VCS_REF="$(git rev-parse HEAD)" \
  -t pullbox-byparr-wait:local .
docker run --rm -i --shm-size 512m --entrypoint /app/.venv/bin/python -e BROWSER_LOCALE=en-US \
  pullbox-byparr-wait:local - < docker/byparr/test_browser_wait.py
```

The six stdlib regression tests run during the build. The browser tests use
intercepted synthetic pages, not external catalogs. Live search checks remain
separate. Before deployment compare image vulnerabilities to the exact base;
existing risk in this resolver is separate from Pullbox/provider exceptions.
Preserve the old image and compose override for rollback. Do not replace the
resolver's settings, identity, order, network, or credentials.
Keep the deployment's 512 MiB shared-memory allocation. Real source checks must
use the deployment's locale settings, not the synthetic test override above.
