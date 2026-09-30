"""Check whether a newer dtxWiki release is available.

Source seam: a private PEP 503 *simple* index NOW (parse the ``dtxwiki/`` listing),
PyPI JSON LATER (a no-op switch — just clear ``DTXWIKI_UPDATE_INDEX_URL``).

Always fail-open and non-blocking: any network/parse error returns the cached
value or ``None`` and never raises, so neither ``dtxwiki info`` nor server
startup can be broken by a flaky index. Results are cached for 24h.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from backend.app import versioning
from backend.app.data_dir import data_dir as default_data_dir
from backend.app.settings import Settings

CACHE_NAME = "update_check.json"
CACHE_TTL_SEC = 24 * 60 * 60
PYPI_JSON_URL = "https://pypi.org/pypi/dtxwiki/json"
PACKAGE_NAME = "dtxwiki"
_REQUEST_TIMEOUT_SEC = 10.0

_WHEEL_RE = re.compile(r"dtxwiki-([^-/\"'>]+)-py3-none-any\.whl", re.IGNORECASE)
_SDIST_RE = re.compile(r"dtxwiki-([^/\"'>]+)\.tar\.gz", re.IGNORECASE)
_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    update_available: bool
    channel: str
    index_url: str | None


def check_for_update(
    settings: Settings,
    *,
    force: bool = False,
    allow_network: bool = True,
    data_dir: Path | None = None,
    now=time.time,
) -> UpdateInfo | None:
    """Return update info, or ``None`` if disabled / unknown.

    ``allow_network=False`` (used at server startup / for the admin banner) reads
    only the cache so the hot path never makes a network call. ``force`` ignores
    a fresh cache and re-fetches.
    """

    if settings.no_update_notifier:
        return None

    current = settings.app_version
    cache_path = _cache_path(data_dir)
    cached = _read_cache(cache_path)
    # Only reuse a cache entry written for the SAME channel + index, otherwise a
    # one-off `--channel prerelease`/private-index check would leak into the
    # stable/default banner.
    if cached is not None and not _cache_matches(cached, settings):
        cached = None

    latest: str | None = None
    if not force and cached is not None and _is_fresh(cached, now):
        latest = cached.get("latest")

    if latest is None and allow_network:
        fetched = _fetch_latest(settings.update_index_url, settings.update_channel)
        if fetched is not None:
            latest = fetched
            _write_cache(cache_path, settings, fetched, now)
        elif cached is not None:
            # Online fetch failed: fall back to the last *matching* cache (stale
            # is better than nothing for an explicit check). The cache-only path
            # (allow_network=False) deliberately does NOT reach here, so the
            # banner never serves stale/mismatched data.
            latest = cached.get("latest")

    if not latest:
        return None

    return UpdateInfo(
        current_version=current,
        latest_version=latest,
        update_available=versioning.is_newer(latest, current),
        channel=settings.update_channel,
        index_url=settings.update_index_url,
    )


def _fetch_latest(index_url: str | None, channel: str) -> str | None:
    try:
        if index_url:
            url = index_url.rstrip("/") + f"/{PACKAGE_NAME}/"
            resp = httpx.get(
                url, timeout=_REQUEST_TIMEOUT_SEC, follow_redirects=True
            )
            resp.raise_for_status()  # a login/error page must not parse as an index
            return pick_max_version(versions_from_simple_index(resp.text), channel)
        resp = httpx.get(PYPI_JSON_URL, timeout=_REQUEST_TIMEOUT_SEC)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict):
            return None
        live = _non_yanked_versions(payload.get("releases", {}))
        return pick_max_version(live, channel)
    except Exception:
        return None


def _non_yanked_versions(releases: dict) -> list[str]:
    """Versions with at least one non-yanked file (PEP 592); skip fully-yanked."""

    live: list[str] = []
    for version, files in releases.items():
        if not isinstance(files, list):
            live.append(version)  # unknown shape -> be lenient
            continue
        if not files:
            continue  # no installable files
        if all(isinstance(f, dict) and f.get("yanked") for f in files):
            continue  # every file yanked
        live.append(version)
    return live


def versions_from_simple_index(html: str) -> set[str]:
    """Extract dtxwiki version strings from a PEP 503 simple-index page.

    Anchors carrying ``data-yanked`` (PEP 592) are skipped. The filename appears
    in the anchor's ``href`` (and usually its text), so each anchor tag is scanned
    individually; if no anchors are found the whole page is scanned as a fallback.
    """

    anchors = _ANCHOR_RE.findall(html)
    if not anchors:
        return set(_WHEEL_RE.findall(html)) | set(_SDIST_RE.findall(html))
    versions: set[str] = set()
    for attrs, text in anchors:
        if "data-yanked" in attrs.lower():
            continue
        blob = attrs + " " + text
        versions.update(_WHEEL_RE.findall(blob))
        versions.update(_SDIST_RE.findall(blob))
    return versions


def pick_max_version(version_strings, channel: str) -> str | None:
    """Largest parseable version; pre-releases excluded unless channel=prerelease."""

    candidates: list[tuple] = []
    for raw in version_strings:
        parsed = versioning.parse_version(raw)
        if parsed is None:
            continue
        if channel != "prerelease" and parsed.is_prerelease:
            continue
        candidates.append((parsed, raw))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1]


def _cache_path(data_dir: Path | None) -> Path:
    return (data_dir or default_data_dir()) / CACHE_NAME


def _read_cache(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(path: Path, settings: Settings, latest: str, now) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "latest": latest,
                    "channel": settings.update_channel,
                    "index_url": settings.update_index_url,
                    "checked_epoch": now(),
                    "checked_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _is_fresh(cached: dict, now) -> bool:
    checked = cached.get("checked_epoch")
    if not isinstance(checked, (int, float)):
        return False
    age = now() - checked
    return 0 <= age < CACHE_TTL_SEC  # future-dated (clock skew) -> treat as stale


def _cache_matches(cached: dict, settings: Settings) -> bool:
    """True if the cache was written for the current channel + index."""

    return (
        cached.get("channel") == settings.update_channel
        and cached.get("index_url") == settings.update_index_url
    )
