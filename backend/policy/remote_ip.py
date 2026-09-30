from __future__ import annotations

import ipaddress

from fastapi import Request

from backend.app.settings import Settings


def resolve_remote_ip(request: Request, settings: Settings | None = None) -> str | None:
    settings = settings or request.app.state.settings
    peer = request.client.host if request.client else None
    if not (
        settings.behind_proxy
        and _ip_in_allowlist(peer, settings.forwarded_allow_ips)
    ):
        return peer

    # Trusted proxy hop. X-Real-IP is honored only when the operator opted in
    # (DTXWIKI_TRUST_X_REAL_IP=1, asserting the proxy OVERWRITES the header):
    # a proxy that merely forwards it would let clients spoof the identity used
    # for rate limits / metrics allowlist / audit logs. Otherwise parse the
    # X-Forwarded-For chain from the RIGHT and return the first hop that is NOT
    # itself a trusted proxy — a client-supplied leftmost value cannot spoof.
    if settings.trust_x_real_ip:
        real_ip = (request.headers.get("x-real-ip") or "").strip()
        if real_ip:
            return real_ip

    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
        for hop in reversed(hops):
            if not _ip_in_allowlist(hop, settings.forwarded_allow_ips):
                return hop
        if hops:
            return hops[0]  # every hop is a trusted proxy -> leftmost is origin
    return peer


def _ip_in_allowlist(candidate: str | None, allowlist: str) -> bool:
    """True if ``candidate`` matches the allowlist (exact, CIDR, or ``*``)."""

    if not candidate:
        return False
    if allowlist.strip() == "*":
        return True
    try:
        candidate_ip = ipaddress.ip_address(candidate)
    except ValueError:
        candidate_ip = None

    for entry in allowlist.split(","):
        value = entry.strip()
        if not value:
            continue
        if candidate == value:
            return True
        if candidate_ip is None:
            continue
        try:
            if "/" in value:
                if candidate_ip in ipaddress.ip_network(value, strict=False):
                    return True
            elif candidate_ip == ipaddress.ip_address(value):
                return True
        except ValueError:
            continue
    return False


# Backwards-compatible alias (peer-trust check is allowlist membership).
def _trusted_forwarding_peer(peer: str | None, allowlist: str) -> bool:
    return _ip_in_allowlist(peer, allowlist)
