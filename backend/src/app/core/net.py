"""Network safety helpers — SSRF guards for tenant-supplied URLs.

Store credentials let a merchant point Speako at an arbitrary base URL
(custom_api_base_url, woocommerce_store_url). Without validation an attacker can
aim those at internal services (cloud metadata 169.254.169.254, localhost Redis,
RFC1918 ranges). validate_public_http_url() rejects non-public targets.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeUrlError(ValueError):
    """Raised when a URL is malformed or resolves to a non-public address."""


def validate_public_http_url(url: str, *, require_https: bool = False) -> str:
    """Return the URL if it is a well-formed http(s) URL that resolves only to
    public IP addresses; otherwise raise UnsafeUrlError.
    """
    if not url or not isinstance(url, str):
        raise UnsafeUrlError("URL is empty")

    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise UnsafeUrlError("URL must use http or https")
    if require_https and scheme != "https":
        raise UnsafeUrlError("URL must use https")

    host = parsed.hostname
    if not host:
        raise UnsafeUrlError("URL has no host")

    # Resolve every address the host maps to; reject if ANY is non-public
    # (defends against split-horizon DNS pointing one record at an internal IP).
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Host does not resolve: {host}") from exc

    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            raise UnsafeUrlError(f"Unresolvable address: {ip_str}")
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local      # 169.254.0.0/16 — cloud metadata
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise UnsafeUrlError(f"URL resolves to a non-public address: {ip_str}")

    return url.strip()
