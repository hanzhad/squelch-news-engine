"""URL canonicalization — the basis for deduplication across sources.

The same article routinely arrives from several feeds with different tracking
tails. Everything that does not change *which document* is being addressed gets
stripped, so that two such links collapse to one uid.
"""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Prefixes and exact names of query params that never identify the document.
_TRACKING_PREFIXES = ("utm_", "_hs", "pk_", "mtm_", "vero_", "hsa_")
_TRACKING_EXACT = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "yclid",
        "msclkid",
        "twclid",
        "igshid",
        "igsh",
        "mc_cid",
        "mc_eid",
        "ref_src",
        "ref_url",
        "cmpid",
        "campaign_id",
        "at_medium",
        "at_campaign",
        "spm",
        "share_id",
    }
)


def _is_tracking(key: str) -> bool:
    lowered = key.lower()
    return lowered in _TRACKING_EXACT or lowered.startswith(_TRACKING_PREFIXES)


def canonicalize(url: str) -> str:
    """Return a stable, comparable form of ``url``.

    Lowercases the scheme and host, drops ``www.``, removes tracking params and
    the fragment, sorts what is left, and trims a trailing slash.
    """
    parts = urlsplit(url.strip())

    scheme = parts.scheme.lower() or "https"
    host = parts.hostname or ""
    if host.startswith("www."):
        host = host[4:]

    # Keep a non-default port; drop :80 / :443 which carry no information.
    netloc = host
    if parts.port and not (
        (scheme == "http" and parts.port == 80) or (scheme == "https" and parts.port == 443)
    ):
        netloc = f"{host}:{parts.port}"

    kept_params = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k)
    ]
    query = urlencode(sorted(kept_params))

    # A bare host and the same host with a trailing slash are one document, so
    # "/" collapses to "" rather than staying as a distinct path.
    path = parts.path.rstrip("/")

    return urlunsplit((scheme, netloc, path, query, ""))


def url_uid(url: str) -> str:
    """Short stable id for a URL, used as the dedup key and issue marker."""
    return hashlib.sha1(canonicalize(url).encode("utf-8")).hexdigest()[:16]
