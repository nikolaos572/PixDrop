"""The address of the client behind a request, also behind reverse proxies and Cloudflare.

The server runs uvicorn with proxy headers, trusting the proxies in FORWARDED_ALLOW_IPS.

- With FORWARDED_ALLOW_IPS="*" (the default: any peer may be a proxy), the client is, in
  this order: the first address of X-Forwarded-For (the original client, as proxies append
  to the list), Cloudflare's CF-Connecting-IP, and the address of the connection. A proxy
  that sets X-Forwarded-For thus decides, whatever CF-Connecting-IP a client sends.
  Anyone who can reach the server directly can set these headers, so the port should then
  be reachable only through the proxy (see docs/DEPLOYMENT.md).
- With FORWARDED_ALLOW_IPS naming the proxies, uvicorn takes the client from
  X-Forwarded-For only when the connection comes from one of them, skipping the trusted
  proxies listed there, and the headers of other peers are ignored: they cannot fake it.

Headers that do not hold an IP address are ignored.
"""

from __future__ import annotations

import ipaddress

from starlette.requests import HTTPConnection


def client_ip(request: HTTPConnection) -> str:
    """The client's IP address, or "-" when it is unknown."""
    if request.app.state.settings.forwarded_allow_ips.strip() == "*":
        for header in (request.headers.get("x-forwarded-for", "").split(",")[0], request.headers.get("cf-connecting-ip", "")):
            if address := _ip(header):
                return address
    # The connection's address, as uvicorn gives it: from the server, or from a trusted proxy's header.
    return request.client.host if request.client and request.client.host else "-"


def _ip(text: str) -> str | None:
    try:
        return str(ipaddress.ip_address(text.strip()))
    except ValueError:
        return None
