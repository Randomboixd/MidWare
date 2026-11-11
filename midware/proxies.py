"""Trust the forwarding headers, but only from a peer we control.

Behind a TLS terminator (tailscale serve, Caddy, Traefik, nginx) the WSGI app
receives a plain HTTP connection and cannot tell that the client's connection
was encrypted. The real scheme is only available as ``X-Forwarded-Proto``, and
believing that header unconditionally is unsafe: any client can claim "https"
while sending cleartext, which would defeat exactly the check that stops signed
credentials crossing an unencrypted network.

So trust is decided by **peer address**, not by a hop count: only a connection
whose ``REMOTE_ADDR`` falls inside ``MIDWARE_TRUSTED_PROXIES`` (loopback by
default) may rewrite the scheme. A remote client cannot reach a loopback
listener, so it cannot forge the header — and unlike ``MIDWARE_TRUST_PROXY_HOPS``
this keeps working when a second proxy is added in front.
"""

from __future__ import annotations

import ipaddress
import re

# Accept only something that looks like a host[:port] or IPv6 literal; anything
# else is a header injection attempt and is ignored.
_HOST_RE = re.compile(r"^[A-Za-z0-9._\-:\[\]]+$")


class TrustedProxies:
    """WSGI middleware: honour X-Forwarded-* only from trusted peer addresses."""

    def __init__(self, app, trusted, trust_forwarded_for: bool = False):
        self.app = app
        self.networks = tuple(
            ipaddress.ip_network(str(item).strip(), strict=False)
            for item in trusted
            if str(item).strip()
        )
        # X-Forwarded-For decides rate-limit identity, so it stays opt-in.
        self.trust_forwarded_for = trust_forwarded_for

    @staticmethod
    def _first(value: str) -> str:
        return (value or "").split(",", 1)[0].strip()

    @staticmethod
    def _last(value: str) -> str:
        parts = [p.strip() for p in (value or "").split(",") if p.strip()]
        return parts[-1] if parts else ""

    def _trusted(self, peer: str) -> bool:
        try:
            addr = ipaddress.ip_address(peer)
        except ValueError:
            return False
        mapped = getattr(addr, "ipv4_mapped", None)
        if mapped is not None:
            addr = mapped
        for net in self.networks:
            try:
                if addr in net:
                    return True
            except TypeError:
                # v4 address vs v6 network (or the reverse) — simply not a match.
                continue
        return False

    def __call__(self, environ, start_response):
        peer = environ.get("REMOTE_ADDR", "")
        if not peer or not self._trusted(peer):
            return self.app(environ, start_response)

        proto = self._first(environ.get("HTTP_X_FORWARDED_PROTO", "")).lower()
        if proto in ("http", "https"):
            environ["wsgi.url_scheme"] = proto

        host = self._first(environ.get("HTTP_X_FORWARDED_HOST", ""))
        if host and _HOST_RE.match(host):
            environ["HTTP_HOST"] = host

        if self.trust_forwarded_for:
            client = self._last(environ.get("HTTP_X_FORWARDED_FOR", ""))
            if client:
                try:
                    ipaddress.ip_address(client)
                except ValueError:
                    pass
                else:
                    environ["REMOTE_ADDR"] = client

        return self.app(environ, start_response)
