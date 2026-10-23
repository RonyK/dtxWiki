"""Check whether a newer dtxWiki release is available.

Uses PyPI JSON, or a configured private PEP 503 *simple* index. Network and
parse errors fail open; explicit checks have a bounded request timeout. Results
are cached for 24h, and deployment-status reads never access the network.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

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


@dataclass(frozen=True)
class UpdateStatus:
    current_version: str
    latest_version: str | None
    update_available: bool
    check_status: Literal["not_checked", "succeeded", "failed", "disabled"]
    checked_at: datetime | None
    stale: bool
    source: Literal["pypi", "configured_index"]


def get_update_status(
    settings: Settings,
    *,
    force: bool = False,
    allow_network: bool = False,
    data_dir: Path | None = None,
    now=time.time,
) -> UpdateStatus:
    """Describe the last successful check and optionally refresh it.

    Unlike the CLI's compatibility API below, stale or failed checks never
    recommend an update. A failed attempt preserves the successful cache and
    its timestamp; failure history belongs to the current response only.
    """

    source = "configured_index" if settings.update_index_url else "pypi"
    if settings.no_update_notifier:
        return UpdateStatus(
            settings.app_version, None, False, "disabled", None, False, source
        )

    cache_path = _cache_path(data_dir)
    cached = _read_cache(cache_path)
    if cached is not None and not _cache_matches(cached, settings):
        cached = None
    # A future success timestamp is not reliable evidence for the admin UI.
    if cached is not None and cached["checked_epoch"] > now():
        cached = None
    latest = cached["latest"] if cached is not None else None
    checked_at = (
        datetime.fromtimestamp(cached["checked_epoch"], UTC) if cached else None
    )
    stale = cached is not None and not _is_fresh(cached, now)
    status: Literal["not_checked", "succeeded", "failed", "disabled"] = (
        "succeeded" if cached is not None else "not_checked"
    )

    if allow_network and (force or cached is None or stale):
        fetched = _fetch_latest(settings.update_index_url, settings.update_channel)
        if fetched is None:
            status = "failed"
        else:
            latest = fetched
            checked_epoch = now()
            checked_at = datetime.fromtimestamp(checked_epoch, UTC)
            stale = False
            status = "succeeded"
            _write_cache(cache_path, settings, fetched, lambda: checked_epoch)

    return UpdateStatus(
        current_version=settings.app_version,
        latest_version=latest,
        update_available=(
            status == "succeeded"
            and not stale
            and versioning.is_newer(latest, settings.app_version)
        ),
        check_status=status,
        checked_at=checked_at,
        stale=stale,
        source=source,
    )


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
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(cached, dict):
        return None
    latest = cached.get("latest")
    if not isinstance(latest, str):
        return None
    parsed_latest = versioning.parse_version(latest)
    if parsed_latest is None:
        return None
    channel = cached.get("channel")
    if not isinstance(channel, str) or channel not in {"stable", "prerelease"}:
        return None
    if channel == "stable" and parsed_latest.is_prerelease:
        return None
    checked = cached.get("checked_epoch")
    if isinstance(checked, bool) or not isinstance(checked, (int, float)):
        return None
    try:
        if not math.isfinite(checked):
            return None
        datetime.fromtimestamp(checked, UTC)
    except (ValueError, OverflowError, OSError):
        return None
    if "source_fingerprint" in cached:
        fingerprint = cached["source_fingerprint"]
        if not isinstance(fingerprint, str) or not re.fullmatch(
            r"[0-9a-f]{64}", fingerprint
        ):
            return None
    elif "index_url" not in cached or (
        cached["index_url"] is not None and not isinstance(cached["index_url"], str)
    ):
        return None
    return cached


def _write_cache(path: Path, settings: Settings, latest: str, now) -> None:
    temporary: Path | None = None
    try:
        checked = now()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Readers see either the previous complete value or this complete value.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(
                {
                    "latest": latest,
                    "channel": settings.update_channel,
                    "source_fingerprint": _source_fingerprint(settings),
                    "checked_epoch": checked,
                    "checked_utc": datetime.fromtimestamp(checked, UTC).isoformat(),
                },
                stream,
            )
        temporary.replace(path)
    except OSError:
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _is_fresh(cached: dict, now) -> bool:
    checked = cached.get("checked_epoch")
    if isinstance(checked, bool) or not isinstance(checked, (int, float)):
        return False
    try:
        if not math.isfinite(checked):
            return False
        age = now() - checked
    except OverflowError:
        return False
    return 0 <= age < CACHE_TTL_SEC  # future-dated (clock skew) -> treat as stale


def _cache_matches(cached: dict, settings: Settings) -> bool:
    """True if the cache was written for the current channel + index."""

    if cached.get("channel") != settings.update_channel:
        return False
    if "source_fingerprint" in cached:
        return cached["source_fingerprint"] == _source_fingerprint(settings)
    # Read legacy caches until the next successful refresh replaces their URL.
    return cached.get("index_url") == settings.update_index_url


def _source_fingerprint(settings: Settings) -> str:
    source = settings.update_index_url or PYPI_JSON_URL
    return hashlib.sha256(source.encode("utf-8")).hexdigest()
