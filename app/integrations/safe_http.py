"""Outbound requests to configured providers: strict URLs, strict timeouts, no redirects.

The bank never follows a URL it did not get from its own configuration or from an origin that
configuration names, so metadata from a provider cannot point it at an internal address.
"""

from collections.abc import Mapping
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit

import httpx

TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 256 * 1024


class UnsafeUrlError(Exception):
    """A URL was refused. The message is a machine code and never repeats the URL's query."""


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def check_url(url: str, *, allowed_origins: frozenset[str], allow_insecure_local: bool) -> str:
    """The URL, if it may be requested. HTTPS and an allow-listed origin, nothing else."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        raise UnsafeUrlError("malformed_url") from None
    if parts.username or parts.password or not host:
        raise UnsafeUrlError("malformed_url")
    if parts.scheme != "https" and not (
        parts.scheme == "http" and allow_insecure_local and _is_loopback(host)
    ):
        raise UnsafeUrlError("insecure_scheme")
    if origin_of(url) not in allowed_origins:
        raise UnsafeUrlError("origin_not_allowed")
    return url


def default_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False)


def get_json(client: httpx.Client, url: str) -> Mapping[str, Any]:
    """A bounded JSON object. Raises `UnsafeUrlError` on anything else."""
    response = client.get(url, headers={"Accept": "application/json"}, follow_redirects=False)
    if response.status_code != 200 or len(response.content) > MAX_RESPONSE_BYTES:
        raise UnsafeUrlError("unexpected_response")
    try:
        data = response.json()
    except ValueError:
        raise UnsafeUrlError("not_json") from None
    if not isinstance(data, Mapping):
        raise UnsafeUrlError("not_json")
    return data
