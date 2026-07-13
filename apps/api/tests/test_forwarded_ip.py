"""The per-IP rate limiter is only as trustworthy as the X-Forwarded-For chain.

uvicorn runs with --forwarded-allow-ips=* (see deploy/Dockerfile.api), which makes
it trust the *leftmost* XFF entry. That's fine as long as the proxy in front of us
overwrites the header. If the proxy ever appends instead, the leftmost entry is
whatever the caller sent, and per-IP limits become per-claimed-IP limits -- i.e.
no limit at all. These tests pin both halves of that contract.
"""

import asyncio
from pathlib import Path

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

NGINX_CONF = Path(__file__).resolve().parents[3] / "deploy" / "nginx.conf"


def _client_seen_by_app(xff: str) -> str:
    """Run a scope through uvicorn's proxy-header middleware the way the deployed
    app does, and report the client IP the rate limiter would end up keying on."""
    seen = {}

    async def app(scope, receive, send):
        seen["client"] = scope["client"]

    middleware = ProxyHeadersMiddleware(app, trusted_hosts="*")
    scope = {
        "type": "http",
        "client": ("172.18.0.9", 5000),  # the proxy container
        "headers": [(b"x-forwarded-for", xff.encode())],
    }
    asyncio.run(middleware(scope, None, None))
    return seen["client"][0]


def test_leftmost_xff_entry_wins():
    """Documents the sharp edge: with wildcard trust, uvicorn believes the FIRST
    entry. A proxy that appends would therefore let the caller pick its own IP."""
    assert _client_seen_by_app("1.2.3.4, 203.0.113.77") == "1.2.3.4"


def test_single_entry_chain_is_the_real_peer():
    """What our nginx actually sends: one value, the address it measured itself."""
    assert _client_seen_by_app("203.0.113.77") == "203.0.113.77"


def test_nginx_overwrites_forwarded_for_instead_of_appending():
    """The regression guard. $proxy_add_x_forwarded_for appends the peer to
    whatever the caller supplied, which hands an attacker the leftmost slot --
    the exact slot uvicorn trusts. Reintroducing it silently voids every per-IP
    limit, and nothing else in the suite would notice."""
    # Strip comments first -- the config explains the trap in prose, and we're
    # asserting on what nginx executes, not what it says.
    directives = [
        line for line in NGINX_CONF.read_text().splitlines() if not line.lstrip().startswith("#")
    ]
    conf = "\n".join(directives)
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in conf
    assert "$proxy_add_x_forwarded_for" not in conf
