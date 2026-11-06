"""The agent's only way out: explicit hosts, HTTPS, strict timeouts, a size limit, no redirects.

Metadata comes from parties the agent has not verified yet, so a URL inside it must not be able to
point the agent at an internal address.
"""

from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit

import httpx

TIMEOUT_SECONDS = 5.0
MAX_BYTES = 512 * 1024


class FetchRefused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


class SafeHttp:
    def __init__(
        self,
        allowed_hosts: set[str],
        *,
        allow_local_http: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._allowed = allowed_hosts
        self._allow_local = allow_local_http
        self._client = httpx.Client(
            timeout=TIMEOUT_SECONDS, follow_redirects=False, transport=transport
        )

    def close(self) -> None:
        self._client.close()

    def allow(self, netloc: str) -> None:
        self._allowed.add(netloc)

    def check(self, url: str) -> str:
        try:
            parts = urlsplit(url)
            host = parts.hostname or ""
        except ValueError:
            raise FetchRefused("malformed_url") from None
        if parts.username or parts.password or not host:
            raise FetchRefused("malformed_url")
        if parts.netloc not in self._allowed:
            raise FetchRefused("host_not_allowed")
        if parts.scheme == "https":
            return url
        if parts.scheme == "http" and self._allow_local and _is_loopback(host):
            return url
        raise FetchRefused("insecure_scheme")

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401
        self.check(url)
        try:
            response = self._client.request(method, url, follow_redirects=False, **kwargs)
        except httpx.HTTPError:
            raise FetchRefused("unreachable") from None
        if response.is_redirect:
            raise FetchRefused("redirect_refused")
        if len(response.content) > MAX_BYTES:
            raise FetchRefused("response_too_large")
        return response

    def get_text(self, url: str, **kwargs: Any) -> str:  # noqa: ANN401
        response = self.request("GET", url, **kwargs)
        if response.status_code != 200:
            raise FetchRefused(f"http_{response.status_code}")
        return response.text

    def get_json(self, url: str, **kwargs: Any) -> Any:  # noqa: ANN401
        text = self.get_text(url, **kwargs)
        try:
            return httpx.Response(200, content=text).json()
        except ValueError:
            raise FetchRefused("not_json") from None
