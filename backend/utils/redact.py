"""Credential redaction for database/connection URLs in logs and manifests.

Single source of truth so every log/diagnostic path masks the same credential
shapes (Codex #47): userinfo creds (``scheme://user:PASSWORD@host``) AND
query-string creds (``?password=...``/``&token=...``), e.g. the pg8000 SSL URL
form. Previously three near-identical copies existed (backup_service, cli/main,
app/bootstrap) and only one masked the query-string form.
"""

import re

# Password class is [^/?#] (not [^@]) so an '@' inside the password is spanned
# and the trailing (@) anchors to the last '@' before the host; '/', '?', '#'
# bound the URL authority per RFC 3986. Username class is [^:/]* (zero-or-more)
# so an empty-username form (scheme://:password@host, e.g. redis://:pw@host) is
# still matched and masked.
_USERINFO = re.compile(r"(://[^:/]*:)[^/?#]+(@)")
# Credential keys match as a substring of the parameter name ([^&=#]* on both
# sides), not only the exact [?&]-anchored token, so prefixed/suffixed forms
# (sslpassword, access_token, refresh_token, client_secret, x-api-key) are
# masked too. Over-masking a benign param beats leaking a credential.
_QUERY_CRED = re.compile(
    r"([?&][^&=#]*(?:password|pwd|secret|token|api[_-]?key)[^&=#]*=)[^&]+",
    re.IGNORECASE,
)


def redact_url(url: str) -> str:
    """Return ``url`` with embedded credentials masked, safe for logging."""
    if not url:
        return url
    redacted = _USERINFO.sub(r"\1****\2", url)
    redacted = _QUERY_CRED.sub(r"\1****", redacted)
    return redacted
