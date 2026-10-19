# Source-specific resolver backoff

Resolvers keep their operator-configured fallback order and shared concurrency
limit. A reachable resolver reporting two unsolved challenges or explicit browser
deadlines for the same source origin within five minutes pauses that origin for
five minutes. Requests during that pause fail fast with `resolver_source_backoff`,
allowing the next resolver to run. The pause does not disable other source origins.

After the pause, only one recovery probe is admitted. Success clears that source's
failure history; another source failure renews the pause. Cancellation releases
the probe without recording a failure. Transport errors, authentication errors,
and rate limits retain the existing endpoint-wide circuit behavior.

State is process-local, bounded to 128 origins per resolver, and resets on provider
restart. Query strings and paths are not part of the key. A successful request to
another origin does not clear this origin's backoff. No cookies are persisted.

Anna's API preserves curated challenge, deadline, and backoff explanations. Raw
upstream messages, credentials, cookies, and speculative IP-block claims are not
returned to clients. Malformed error envelopes remain generic resolver failures.
An unsuccessful LibGen catalog fallback cannot turn a known resolver failure into
an empty success or a generic outer deadline. It reserves half a second to return
the original failure; a successful catalog fallback remains available.

## Isolated TRAWL comparison, 2026-10-19

Two temporary containers used the exact deployed TRAWL 1.8.0 image, the same
network and 512 MiB shared memory, one browser each, and no published ports.
The sole experimental change disabled forced re-navigation in the DDoS-Guard
waiter (`maxRenavigations` default 2 to 0). Both used a 30-second scrape budget,
browser-only tier 3, and the same Anna search filters. No downloads were started.

| Order | Query | Variant | Outcome | TRAWL total time |
| --- | --- | --- | --- | --- |
| 1 | Batman Vol 1 Failsafe | Stock | DDoS-Guard challenge persisted | 24.666 s |
| 2 | Batman Vol 1 Failsafe | No forced re-navigation | Deadline exceeded | 30.016 s |
| 3 | Miles Morales Brooklyn's Finest 4 | No forced re-navigation | Deadline exceeded | 30.016 s |
| 4 | Miles Morales Brooklyn's Finest 4 | Stock | DDoS-Guard challenge persisted | 24.866 s |

Neither variant produced a usable result page. This small comparison does not
establish the underlying challenge failure or prove an IP ban. Disabling forced
re-navigation did not improve these searches, so the experimental patch was not
deployed. The temporary containers were removed. Source-specific backoff avoids
paying for the repeatedly failing attempt on every search without changing the
saved fallback order or claiming TRAWL now solves Anna's challenge.
