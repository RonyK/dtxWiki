"""Provider-neutral OIDC code-flow and signed-token validation helpers."""

from __future__ import annotations

import base64
import hmac
import math
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit

import jwt

from backend.spi.auth import AuthMethodError
from backend.spi.auth_oidc.transport import JsonTransport, https_url

DEFAULT_SIGNING_ALGORITHMS = ("RS256", "ES256")
SAFE_SIGNING_ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "ES512",
        "EdDSA",
    }
)
SAFE_AUTHORIZATION_PARAMETERS = frozenset(
    {"prompt", "login_hint", "ui_locales", "claims_locales", "acr_values", "max_age"}
)


@dataclass
class _Document:
    value: dict[str, object] | None
    expires: float
    refresh_after: float


class OidcClient:
    """Reusable helper, without registering a provider or creating a session.

    Metadata and keys are bounded per client instance; adapters should retain
    their client instance. Document fetches, including cold misses and failed
    refreshes, are throttled to one attempt per 30 seconds per cached URL.
    """

    def __init__(
        self,
        *,
        transport: JsonTransport | None = None,
        cache_ttl_seconds: float = 300,
        max_cache_entries: int = 128,
        refresh_interval_seconds: float = 30,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 < cache_ttl_seconds <= 3600 or max_cache_entries < 1:
            raise ValueError("invalid OIDC cache limits")
        if refresh_interval_seconds <= 0:
            raise ValueError("positive refresh interval required")
        self.transport = transport or JsonTransport()
        self.cache_ttl = cache_ttl_seconds
        self.max_cache_entries = max_cache_entries
        self.refresh_interval = refresh_interval_seconds
        self.clock = clock
        self._metadata: OrderedDict[tuple, _Document] = OrderedDict()
        self._jwks: OrderedDict[tuple, _Document] = OrderedDict()
        self._lock = threading.RLock()

    def _document(self, cache, url, *, allow_private_network=False, refresh=False):
        key = (url, allow_private_network)
        with self._lock:
            now = self.clock()
            existing = cache.get(key)
            if existing:
                cache.move_to_end(key)
                if (
                    existing.value is not None
                    and now < existing.expires
                    and (not refresh or now < existing.refresh_after)
                ):
                    return existing.value
                if now < existing.refresh_after:
                    raise AuthMethodError("identity_provider_keys_unavailable")
            # Retain a bounded attempt marker before I/O, including a cold
            # miss, so an unavailable IdP cannot make every callback fetch it.
            cache[key] = _Document(
                existing.value if existing else None,
                existing.expires if existing else now,
                now + self.refresh_interval,
            )
            cache.move_to_end(key)
            while len(cache) > self.max_cache_entries:
                cache.popitem(last=False)
            value = self.transport.request(
                "GET", url, allow_private_network=allow_private_network
            )
            cache[key] = _Document(
                value, now + self.cache_ttl, now + self.refresh_interval
            )
            return value

    def discovery(
        self, issuer: str, *, allow_private_network=False
    ) -> dict[str, object]:
        https_url(issuer, issuer=True)
        value = self._document(
            self._metadata,
            issuer.rstrip("/") + "/.well-known/openid-configuration",
            allow_private_network=allow_private_network,
        )
        if value.get("issuer") != issuer:
            raise AuthMethodError("identity_provider_issuer_mismatch")
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            https_url(value.get(name))
        return dict(value)

    def authorization_url(
        self,
        metadata: Mapping,
        *,
        client_id: str,
        redirect_uri: str,
        state: str,
        nonce: str | None,
        code_challenge: str | None,
        scopes: Sequence[str],
        parameters: Mapping[str, str] | None = None,
    ) -> str:
        if (
            not state
            or nonce == ""
            or (
                code_challenge is not None
                and (
                    not isinstance(code_challenge, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{43}", code_challenge)
                )
            )
        ):
            raise AuthMethodError("missing_oidc_transaction_binding")
        parameters = dict(parameters or {})
        if set(parameters) - SAFE_AUTHORIZATION_PARAMETERS:
            raise AuthMethodError("unsafe_oidc_authorization_parameter")
        endpoint = https_url(metadata.get("authorization_endpoint"))
        parsed = urlsplit(endpoint)
        fixed = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            **parameters,
        }
        if nonce is not None:
            fixed["nonce"] = nonce
        if code_challenge is not None:
            fixed.update(code_challenge=code_challenge, code_challenge_method="S256")
        original = parse_qsl(parsed.query, keep_blank_values=True)
        if any(key in fixed for key, _ in original):
            raise AuthMethodError("ambiguous_oidc_authorization_endpoint")
        return urlunsplit(parsed._replace(query=urlencode([*original, *fixed.items()])))

    def exchange_code(
        self,
        metadata: Mapping,
        *,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_verifier: str | None,
        client_secret: str | None = None,
        token_endpoint_auth_method: str = "client_secret_basic",
        allow_private_network: bool = False,
    ) -> dict[str, object]:
        if (
            not code
            or (
                code_verifier is not None
                and (
                    not isinstance(code_verifier, str)
                    or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", code_verifier)
                )
            )
            or (code_verifier is None and token_endpoint_auth_method == "none")
        ):
            raise AuthMethodError("missing_oidc_code_binding")
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        }
        if code_verifier is not None:
            data["code_verifier"] = code_verifier
        headers: dict[str, str] = {}
        if token_endpoint_auth_method == "client_secret_basic":
            if not client_secret:
                raise AuthMethodError("missing_oidc_client_secret")
            # OAuth Basic uses form encoding before base64 (RFC 6749 2.3.1).
            encoded = f"{quote_plus(client_id)}:{quote_plus(client_secret)}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(encoded).decode()
        elif token_endpoint_auth_method in {"client_secret_post", "none"}:
            data["client_id"] = client_id
            if token_endpoint_auth_method == "client_secret_post":
                if not client_secret:
                    raise AuthMethodError("missing_oidc_client_secret")
                data["client_secret"] = client_secret
        else:
            raise AuthMethodError("unsupported_oidc_client_authentication")
        result = self.transport.request(
            "POST",
            https_url(metadata.get("token_endpoint")),
            data=data,
            headers=headers,
            allow_private_network=allow_private_network,
        )
        if "error" in result or not isinstance(result.get("id_token"), str):
            raise AuthMethodError("invalid_oidc_token_response")
        return result

    def _key(self, metadata: Mapping, header: Mapping, *, allow_private_network=False):
        url = https_url(metadata.get("jwks_uri"))
        document = self._document(
            self._jwks, url, allow_private_network=allow_private_network
        )
        for refresh in (False, True):
            if refresh:
                document = self._document(
                    self._jwks,
                    url,
                    allow_private_network=allow_private_network,
                    refresh=True,
                )
            keys = document.get("keys")
            if not isinstance(keys, list) or not 1 <= len(keys) <= 100:
                raise AuthMethodError("invalid_oidc_jwks")
            candidates = [
                key
                for key in keys
                if isinstance(key, dict)
                and (header.get("kid") is None or key.get("kid") == header["kid"])
                and key.get("use", "sig") == "sig"
                and key.get("alg", header["alg"]) == header["alg"]
                and ("key_ops" not in key or key["key_ops"] == ["verify"])
            ]
            if len(candidates) == 1:
                key = jwt.PyJWK(candidates[0], algorithm=header["alg"]).key
                if header["alg"].startswith(("RS", "PS")) and key.key_size < 2048:
                    raise AuthMethodError("weak_oidc_signing_key")
                return key
            if len(candidates) > 1:
                break
        raise AuthMethodError("oidc_signing_key_not_found")

    def validate_id_token(
        self,
        token: str,
        metadata: Mapping,
        *,
        issuer: str,
        client_id: str,
        nonce: str | None,
        accepted_issuers: Sequence[str] | None = None,
        trusted_audiences: Sequence[str] = (),
        algorithms: Sequence[str] = DEFAULT_SIGNING_ALGORITHMS,
        max_age: int | None = None,
        allow_private_network: bool = False,
    ) -> dict[str, object]:
        if (nonce is not None and (not isinstance(nonce, str) or not nonce)) or (
            not isinstance(token, str) or len(token) > 32 * 1024
        ):
            raise AuthMethodError("invalid_oidc_id_token")
        if not algorithms or set(algorithms) - SAFE_SIGNING_ALGORITHMS:
            raise AuthMethodError("unsafe_oidc_signing_algorithm")
        try:
            header = jwt.get_unverified_header(token)
            if (
                header.get("alg") not in algorithms
                or header.get("crit")
                or header.get("b64") is False
                or ("kid" in header and not isinstance(header["kid"], str))
            ):
                raise AuthMethodError("invalid_oidc_id_token_header")
            key = self._key(
                metadata, header, allow_private_network=allow_private_network
            )
            claims = jwt.decode(
                token,
                key,
                algorithms=list(algorithms),
                audience=client_id,
                issuer=list(accepted_issuers or (issuer,)),
                leeway=30,
                options={
                    "require": ["iss", "sub", "aud", "exp", "iat"]
                    + (["nonce"] if nonce is not None else [])
                },
            )
            if (
                not isinstance(claims["sub"], str)
                or not 1 <= len(claims["sub"]) <= 255
                or any(not 0x20 <= ord(char) <= 0x7E for char in claims["sub"])
            ):
                raise ValueError("invalid subject")
            for claim in ("iat", "exp", "nbf", "auth_time"):
                value = claims.get(claim)
                if claim in claims and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                ):
                    raise ValueError("invalid NumericDate")
            aud = claims["aud"]
            if isinstance(aud, str):
                aud = [aud]
            if (
                not isinstance(aud, list)
                or not aud
                or any(not isinstance(value, str) or not value for value in aud)
            ):
                raise ValueError("invalid audience")
            if (len(aud) > 1 or "azp" in claims) and claims.get("azp") != client_id:
                raise ValueError("invalid authorized party")
            if set(aud) - {client_id, *trusted_audiences}:
                raise ValueError("untrusted additional audience")
            if nonce is not None and (
                not isinstance(claims["nonce"], str)
                or not hmac.compare_digest(claims["nonce"].encode(), nonce.encode())
            ):
                raise ValueError("nonce mismatch")
            if "auth_time" in claims and claims["auth_time"] > time.time() + 30:
                raise ValueError("future authentication")
            if max_age is not None and (
                "auth_time" not in claims
                or time.time() - claims["auth_time"] > max_age + 30
            ):
                raise ValueError("authentication too old")
            return claims
        except AuthMethodError:
            raise
        except (
            jwt.PyJWTError,
            ValueError,
            TypeError,
            KeyError,
            RecursionError,
            OverflowError,
        ):
            raise AuthMethodError("invalid_oidc_id_token") from None

    def fetch_profile(
        self,
        endpoint: str,
        access_token: str,
        *,
        expected_subject: str | None = None,
        allow_private_network: bool = False,
    ) -> dict[str, object]:
        """Optional OAuth2 JSON profile/UserInfo fetch, never an identity by itself."""
        if (
            not isinstance(access_token, str)
            or not access_token
            or any(ord(char) <= 32 or ord(char) >= 127 for char in access_token)
        ):
            raise AuthMethodError("invalid_oauth2_access_token")
        profile = self.transport.request(
            "GET",
            https_url(endpoint),
            headers={"Authorization": "Bearer " + access_token},
            allow_private_network=allow_private_network,
        )
        if expected_subject is not None and profile.get("sub") != expected_subject:
            raise AuthMethodError("oidc_userinfo_subject_mismatch")
        return profile
