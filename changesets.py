"""Resolve which OSM changesets carry a given hashtag, via the OSM changeset API.

Overpass does not store changeset comments, so the ``--highlight-hashtag`` filter
cannot be answered from the feature download alone. Instead, the meta-aware fetch
records each feature's ``changeset`` id, and this module maps those ids to their
changeset ``comment`` / ``hashtags`` tags through the official OSM API
(``/api/0.6/changesets``), returning the subset whose tags match the hashtag.

Lookups are cached per changeset so continent-scale reruns stay cheap and only the
newly-seen changesets are fetched.
"""

from __future__ import annotations

import re
import time
from typing import Iterable

from common import cache_get, cache_key, cache_set

OSM_CHANGESET_API = "https://api.openstreetmap.org/api/0.6/changesets"
# The API accepts at most 100 ids per `changesets=` request.
MAX_IDS_PER_REQUEST = 100
USER_AGENT = "GridToPoster/1.0 (+https://github.com/open-energy-transition/grid2poster)"


def _coerce_ids(changeset_ids: Iterable) -> list[int]:
    """Dedupe, drop None/NaN, and coerce changeset ids to int (sorted for stable batching)."""
    seen: set[int] = set()
    for raw in changeset_ids:
        if raw is None:
            continue
        try:
            cid = int(raw)
        except (TypeError, ValueError):
            continue
        if cid <= 0:
            continue
        seen.add(cid)
    return sorted(seen)


def _hashtag_matches(record: dict, hashtag_lower: str) -> bool:
    """True when a changeset's comment/hashtags tags contain ``hashtag_lower``.

    ``hashtags`` is the editor-populated ``;``-separated list; ``comment`` is the
    free-text message. Match is case-insensitive and the leading ``#`` is optional.
    """
    for token in (record.get("hashtags") or "").split(";"):
        token = token.strip().lstrip("#").lower()
        if token and token == hashtag_lower:
            return True
    comment = (record.get("comment") or "").lower()
    if comment:
        # Match "#tag" as a whole word so "#mapyourgrid" does not also hit
        # "#mapyourgridded" or a bare substring inside another word.
        if re.search(r"(?<![\w#])#" + re.escape(hashtag_lower) + r"\b", comment):
            return True
    return False


def _fetch_changeset_records(
    ids: list[int], request_delay: float, timeout: int
) -> dict[int, dict]:
    """Fetch comment/hashtags for ``ids`` from the OSM API in batches of 100.

    Returns a mapping id -> {"comment", "hashtags"}. Ids absent from a response
    (deleted/hidden) are recorded as empty so they are cached and not refetched.
    """
    import requests as http_requests

    records: dict[int, dict] = {}
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    for start in range(0, len(ids), MAX_IDS_PER_REQUEST):
        batch = ids[start : start + MAX_IDS_PER_REQUEST]
        batch_number = start // MAX_IDS_PER_REQUEST + 1
        total_batches = (len(ids) + MAX_IDS_PER_REQUEST - 1) // MAX_IDS_PER_REQUEST
        print(f"  Changeset API batch {batch_number:,}/{total_batches:,} ({len(batch)} ids)")

        param = ",".join(str(cid) for cid in batch)
        attempt = 0
        while True:
            attempt += 1
            try:
                response = http_requests.get(
                    OSM_CHANGESET_API,
                    params={"changesets": param},
                    headers=headers,
                    timeout=timeout,
                )
            except http_requests.RequestException as exc:
                wait = min(120, 5 * attempt)
                print(f"    Request failed ({exc}); retrying in {wait}s")
                time.sleep(wait)
                continue
            if response.status_code in (429, 503, 504):
                wait = min(180, 10 * attempt)
                print(f"    OSM API busy (HTTP {response.status_code}); waiting {wait}s")
                time.sleep(wait)
                continue
            response.raise_for_status()
            break

        for changeset in response.json().get("changesets", []):
            cid = int(changeset["id"])
            tags = changeset.get("tags", {})
            records[cid] = {
                "comment": tags.get("comment"),
                "hashtags": tags.get("hashtags"),
            }
        # Record ids the API omitted so they cache as empty rather than refetch.
        for cid in batch:
            records.setdefault(cid, {"comment": None, "hashtags": None})

        if request_delay > 0 and start + MAX_IDS_PER_REQUEST < len(ids):
            time.sleep(request_delay)

    return records


def resolve_highlight_changesets(
    changeset_ids: Iterable,
    hashtag: str,
    use_cache: bool = True,
    request_delay: float = 1.0,
    timeout: int = 60,
) -> set[int]:
    """Return the subset of ``changeset_ids`` whose comment/hashtags carry ``hashtag``.

    Each changeset's tags are cached individually, so only ids never looked up
    before hit the network. ``hashtag`` may be passed with or without a leading
    ``#`` and is matched case-insensitively.
    """
    hashtag_lower = hashtag.strip().lstrip("#").lower()
    if not hashtag_lower:
        return set()

    ids = _coerce_ids(changeset_ids)
    if not ids:
        return set()

    records: dict[int, dict] = {}
    uncached: list[int] = []
    for cid in ids:
        if use_cache:
            cached = cache_get(cache_key("changeset_hashtag_v1", cid))
            if cached is not None:
                records[cid] = cached
                continue
        uncached.append(cid)

    if records:
        print(f"  Reused {len(records):,}/{len(ids):,} changeset(s) from cache")
    if uncached:
        print(f"  Looking up {len(uncached):,} changeset(s) via the OSM changeset API")
        fetched = _fetch_changeset_records(uncached, request_delay, timeout)
        for cid, record in fetched.items():
            cache_set(cache_key("changeset_hashtag_v1", cid), record)
        records.update(fetched)

    matched = {cid for cid, record in records.items() if _hashtag_matches(record, hashtag_lower)}
    print(f"  {len(matched):,} changeset(s) match #{hashtag_lower}")
    return matched
