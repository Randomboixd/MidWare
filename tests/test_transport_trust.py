"""Transport trust: X-Forwarded-Proto is believed only from a trusted peer.

The bug this covers: behind a TLS terminator (tailscale serve, Caddy, nginx) the
app receives plain HTTP, so ``request.is_secure`` is False and the SMA guard
answers 403 insecure_transport even though the client's connection was
encrypted. Believing the header unconditionally would be worse — any client
could claim "https" over cleartext, which is the exact thing the guard exists to
refuse. So the peer address decides.
"""

from __future__ import annotations

from midware import create_app
from midware.proxies import TrustedProxies

LOOPBACK_TRUST = ("127.0.0.1/32", "::1/128")


def _app(**overrides):
    config = {
        "TESTING": True,
        "ADMIN_AUTH_ENABLED": False,
        "TRUSTED_PROXIES": LOOPBACK_TRUST,
    }
    config.update(overrides)
    return create_app(database_path=":memory:", config_overrides=config)


def _server_key(client, *, peer, proto=None, host=None):
    headers = {}
    if proto is not None:
        headers["X-Forwarded-Proto"] = proto
    if host is not None:
        headers["X-Forwarded-Host"] = host
    return client.get("/_midware/api/v1/server-key", headers=headers,
                      environ_base={"REMOTE_ADDR": peer})


# -- middleware in isolation -------------------------------------------------


class _Echo:
    """Minimal WSGI app that reports the environ the middleware produced."""

    def __init__(self):
        self.seen = {}

    def __call__(self, environ, start_response):
        self.seen = {
            "scheme": environ.get("wsgi.url_scheme"),
            "host": environ.get("HTTP_HOST"),
            "remote": environ.get("REMOTE_ADDR"),
        }
        start_response("200 OK", [])
        return [b""]


def _call(trusted, peer, *, proto=None, host=None, xff=None, trust_for=False):
    inner = _Echo()
    mw = TrustedProxies(inner, trusted, trust_forwarded_for=trust_for)
    environ = {"REMOTE_ADDR": peer, "wsgi.url_scheme": "http", "HTTP_HOST": "backend:80"}
    if proto:
        environ["HTTP_X_FORWARDED_PROTO"] = proto
    if host:
        environ["HTTP_X_FORWARDED_HOST"] = host
    if xff:
        environ["HTTP_X_FORWARDED_FOR"] = xff
    mw(environ, lambda *a, **k: None)
    return inner.seen


def test_loopback_peer_can_rewrite_scheme():
    assert _call(LOOPBACK_TRUST, "127.0.0.1", proto="https")["scheme"] == "https"


def test_ipv6_loopback_peer_can_rewrite_scheme():
    assert _call(LOOPBACK_TRUST, "::1", proto="https")["scheme"] == "https"


def test_ipv4_mapped_loopback_is_trusted():
    assert _call(LOOPBACK_TRUST, "::ffff:127.0.0.1", proto="https")["scheme"] == "https"


def test_remote_peer_cannot_rewrite_scheme():
    seen = _call(LOOPBACK_TRUST, "203.0.113.5", proto="https")
    assert seen["scheme"] == "http"


def test_missing_header_leaves_scheme_alone():
    assert _call(LOOPBACK_TRUST, "127.0.0.1")["scheme"] == "http"


def test_nonsense_proto_is_ignored():
    assert _call(LOOPBACK_TRUST, "127.0.0.1", proto="gopher")["scheme"] == "http"


def test_trusted_peer_may_set_host():
    assert _call(LOOPBACK_TRUST, "127.0.0.1", host="midware.example.com")["host"] == "midware.example.com"


def test_host_with_injection_attempt_is_ignored():
    assert _call(LOOPBACK_TRUST, "127.0.0.1", host="evil\r\nX-Bad: 1")["host"] == "backend:80"


def test_forwarded_for_ignored_by_default():
    seen = _call(LOOPBACK_TRUST, "127.0.0.1", xff="198.51.100.7")
    assert seen["remote"] == "127.0.0.1"


def test_forwarded_for_honoured_when_opted_in():
    seen = _call(LOOPBACK_TRUST, "127.0.0.1", xff="198.51.100.7, 10.0.0.1", trust_for=True)
    # Last entry: the one appended by the proxy that actually talked to us.
    assert seen["remote"] == "10.0.0.1"


def test_forwarded_for_from_remote_peer_still_ignored():
    seen = _call(LOOPBACK_TRUST, "203.0.113.5", xff="198.51.100.7", trust_for=True)
    assert seen["remote"] == "203.0.113.5"


# -- through the actual app: the bug that started this -----------------------


def test_sma_accepts_proxied_https_from_loopback():
    client = _app().test_client()
    resp = _server_key(client, peer="127.0.0.1", proto="https")
    assert resp.status_code == 200
    assert resp.get_json()["transport"] == "tls"


def test_sma_refuses_spoofed_https_from_a_remote_peer():
    client = _app().test_client()
    resp = _server_key(client, peer="203.0.113.5", proto="https")
    assert resp.status_code == 403
    assert resp.get_json()["error"]["code"] == "insecure_transport"


def test_sma_refuses_plain_loopback_without_the_header():
    client = _app().test_client()
    resp = _server_key(client, peer="127.0.0.1")
    assert resp.status_code == 403
    assert resp.get_json()["error"]["code"] == "insecure_transport"


def test_sma_refuses_explicit_http_from_loopback():
    client = _app().test_client()
    resp = _server_key(client, peer="127.0.0.1", proto="http")
    assert resp.status_code == 403


def test_address_trust_can_be_disabled():
    client = _app(TRUSTED_PROXIES=()).test_client()
    resp = _server_key(client, peer="127.0.0.1", proto="https")
    assert resp.status_code == 403


def test_extra_cidr_can_be_trusted_for_a_docker_gateway():
    client = _app(TRUSTED_PROXIES=("127.0.0.1/32", "172.17.0.0/16")).test_client()
    resp = _server_key(client, peer="172.17.0.1", proto="https")
    assert resp.status_code == 200
    assert resp.get_json()["transport"] == "tls"


def test_hop_counting_still_works_for_existing_deployments():
    client = _app(TRUST_PROXY_HOPS=1).test_client()
    resp = _server_key(client, peer="203.0.113.5", proto="https")
    assert resp.status_code == 200
