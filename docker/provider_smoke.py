"""Private-network startup smoke for source-provider images."""

from __future__ import annotations

import json
import os
from http.client import HTTPConnection
from urllib.parse import urlsplit


def _get(base_url: str, path: str, token: str) -> dict[str, object]:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"getcomics", "annas-archive", "libgen"}
        or parsed.port != 8780
        or parsed.path
    ):
        raise RuntimeError("Provider smoke target is invalid.")
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    try:
        connection.request(
            "GET",
            path,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"Provider returned HTTP {response.status}.")
        payload = json.loads(response.read())
    finally:
        connection.close()
    if not isinstance(payload, dict):
        raise RuntimeError("Provider returned an invalid JSON envelope.")
    return {str(key): value for key, value in payload.items()}


def main() -> None:
    token = os.environ["PULLBOX_PROVIDER_TOKEN"]
    expected = {
        os.environ["GETCOMICS_URL"]: "pullbox.getcomics",
        os.environ["ANNAS_ARCHIVE_URL"]: "pullbox.annas_archive",
        os.environ["LIBGEN_URL"]: "pullbox.libgen",
    }
    for base_url, provider_id in expected.items():
        manifest = _get(base_url, "/v1/manifest", token)
        if manifest.get("provider_id") != provider_id:
            raise RuntimeError("Provider identity did not match the expected image.")


if __name__ == "__main__":
    main()
