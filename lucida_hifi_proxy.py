"""
HiFi-API-compatible proxy backed by lucida.to (via the bundled lucidadl).

Exposes the subset of the HiFi API (https://github.com/binimum/hifi-api) that
music-sync clients such as SoulSync actually call, plus valid empty responses
for the rest of the surface so an unknown route never breaks a client.

Client contracts this file deliberately honours (verified against SoulSync's
core/hifi_client.py):

  * Ids handed out by /search/ are integers, because the client's download path
    runs `int(track_id_str)` on them.
  * /search/ accepts `s`, `a` AND `al` as alternative query fields; a required
    `s` would answer 422 to artist/album-only searches.
  * /trackManifests/ must return a non-empty data.data.attributes.uri for the
    health-probe id, before any search has populated the cache.
  * The HLS playlist holds ONE segment: the whole audio file. Clients fetch the
    playlist, then GET that segment and write the bytes straight to disk.
  * The instance URL is pasted, not parsed: every call is built as
    `url = f"{instance}{path}"` with no urljoin or host normalisation, so a
    path prefix like `http://host:8002/qobuz` reaches the prefixed routes as
    `http://host:8002/qobuz/search/`. That is what makes the whole
    per-service routing below possible without touching the client.

Per-service routing: every route is mounted three times - bare, under
`/{service}`, and under `/{service}/{country}` - so `http://localhost:8002/qobuz`
is a usable SoulSync instance pinned to one lucida.to service, and
`http://localhost:8002/qobuz/GB` additionally pins the search country.

Ownership: `MetadataCache` owns the client-facing id space, `LucidaWrapper`
owns every lucida.to call, `Target` owns which service/country a request is
for, and the routes own the HTTP contract (status codes, response shapes, and
the error/no-match policy).
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import tempfile
from base64 import b64encode
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from lucidadl import api, utils
from lucidadl.session import acquire_clearance, chromium_installed, install_chromium

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_VERSION = "2.10"

# lucida.to service used for keyword search. Defaults to qobuz because it is
# the lossless source, which is the point of a HiFi proxy. lucida.to disables
# services server-side without notice, so availability varies over time.
SERVICE = os.getenv("LUCIDA_SERVICE", "qobuz")

# Every service lucida.to itself offers, read from the <select id="service"> on
# its own search page rather than assumed. A name outside this list is still
# tried first, but it is reported loudly rather than failing quietly.
# Note there is no `spotify`: it is no longer in the dropdown at all.
KNOWN_SERVICES = (
    "amazon", "deezer", "grilledcheese", "qobuz", "soundcloud", "tidal", "yandex",
)

# Country to search each service with, or "" to send none. lucida.to rejects any
# country a service does not accept with a plain "Invalid country for X", and
# the accepted set differs per service - it is NOT one global country.
#
# This table is only a BOOTSTRAP value. It is overridden at runtime by
# `discover_countries()`, which reads the accepted set out of lucida's own
# search response. A hardcoded table is exactly what went stale before: an
# earlier revision of this file shipped qobuz -> "US", while qobuz accepts GB
# only, so every search on the default service failed and the fallback chain
# quietly answered instead. Measured 2026-10-01 from lucida's own
# <select id="country">:
#   qobuz     GB only
#   soundcloud, grilledcheese   XX only
#   amazon    48 countries (US is one); its search backend is broken separately
#   tidal, deezer      no accepted country configured at all
BOOTSTRAP_COUNTRY = {
    "qobuz": "GB",
    "soundcloud": "XX",
    "grilledcheese": "XX",
    "amazon": "US",
}

# Filled in at runtime by `LucidaWrapper.discover_countries()` from the live
# service, and reported by /health. Empty until a service has been used.
_country_by_service: Dict[str, List[str]] = {}
_country_lock = asyncio.Lock()

# Fallback order for a HiFi proxy, so lossless sources are preferred and lossy
# ones are a last resort: qobuz and grilledcheese both returned verified FLAC,
# while soundcloud serves lossy audio and reports the uploader as the artist.
# amazon (backend ENOENT), yandex ("currently disabled") and tidal/deezer are
# left out because each was measured to fail every search, and retrying a dead
# service costs a wasted upstream round-trip on every search. Set LUCIDA_SERVICE
# to one of them explicitly to use it anyway.
FALLBACK_SERVICES = ("qobuz", "grilledcheese", "soundcloud")
# Whether a request that arrived under a /{service} prefix may fall back to
# another service when the pinned one is broken. Off by default: a prefix is an
# explicit pin, and quietly answering from a different service would defeat it -
# it is also exactly what SoulSync's own instance rotation already does, one
# layer up, when the whole instance fails. Set to 1 to opt back in.
PATH_FALLBACK = os.getenv("LUCIDA_PATH_FALLBACK", "").strip().lower() in ("1", "true", "yes", "on")
# Account/country to resolve item pages and start downloads with. Empty by
# default, which sends the literal "auto" in the load request and lets lucida
# pick the account. A specific value is not rejected, but the item route was
# measured ignoring `country` entirely (GB, US and FR all resolved against the
# same account), so pinning one here buys nothing and can only go stale.
COUNTRY = os.getenv("LUCIDA_COUNTRY", "")
USER_AGENT = os.getenv(
    "LUCIDA_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)
# Must be the address the CLIENT can reach (LAN IP when SoulSync runs elsewhere).
PROXY_BASE_URL = os.getenv("PROXY_BASE_URL", "http://host.docker.internal:8002").rstrip("/")

MAX_CACHE_SIZE = 1000
# Tidal track id the client uses to probe whether an instance can download.
SOULSYNC_PROBE_ID = 1550546
PROBE_QUERY = os.getenv("LUCIDA_PROBE_QUERY", "test")
# EXTINF for the single full-file segment. lucida exposes no duration, so this
# is a placeholder; a client's preview guard only rejects a playlist SHORTER
# than the track, never a longer one.
MANIFEST_DURATION = 3600.0

# An explicit DOWNLOAD_DIR is used as-is. The fallback still keeps every file in
# one dedicated folder rather than scattering them through the system temp dir.
_download_dir = os.getenv("DOWNLOAD_DIR")
DOWNLOAD_DIR = (
    Path(_download_dir) if _download_dir else Path(tempfile.gettempdir()) / "lucida_proxy"
)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("lucida-proxy")

_unknown_service_reported = False


def resolved_service() -> str:
    """Configured lucida.to service, with a one-time report if it is unrecognized.

    A typo in LUCIDA_SERVICE is otherwise invisible: the value is tried first,
    fails, and the proxy answers from a fallback service while the error text
    names whichever service happened to fail last. Unknown names are still
    honoured, because lucida.to adds and retires services without notice.
    """
    global _unknown_service_reported

    name = str(SERVICE).strip().lower()
    if name not in KNOWN_SERVICES and not _unknown_service_reported:
        _unknown_service_reported = True
        logger.error(
            "LUCIDA_SERVICE=%r is not a service this proxy recognizes (%s). "
            "It will still be tried first, and search will fall back to another "
            "service if it fails.",
            SERVICE,
            ", ".join(KNOWN_SERVICES),
        )
    return name


def service_country(service: str) -> str:
    """Country to search `service` with, or "" to send no country param.

    Prefers what the live service reported over `BOOTSTRAP_COUNTRY`, so a
    service whose accepted countries change needs no code change here.
    """
    name = api.normalize_service(service)
    known = _country_by_service.get(name) or []
    if known:
        return known[0]
    return BOOTSTRAP_COUNTRY.get(name, "")


# ---------------------------------------------------------------------------
# Per-request service/country target
#
# A request's path prefix decides which lucida.to service it searches: `/qobuz`
# pins qobuz, `/qobuz/GB` pins qobuz and the search country, and a bare
# `/search/` keeps the LUCIDA_SERVICE + fallback behaviour it always had.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """Which service (and country) one request is for.

    `prefix` is echoed back into every manifest URL this request produces, which
    is what carries the pin from the search that found a track all the way
    through to the download that fetches it.
    """

    service: str
    country: str
    prefix: str
    pinned: bool

    def base_url(self) -> str:
        """Absolute base for URLs this request hands back to the client."""
        return PROXY_BASE_URL + self.prefix


def validate_service(raw: str) -> str:
    """Normalize a service name from the path, or reject it.

    Rejecting is deliberate. A misspelled service otherwise fails quietly: it is
    tried, fails, and the request is answered from a fallback, so the client
    gets plausible results from a service nobody asked for and the error text
    names whichever service happened to fail last.
    """
    name = api.normalize_service(str(raw).strip().lower())
    if name not in KNOWN_SERVICES:
        raise HTTPException(
            status_code=404,
            detail=(
                f"unknown lucida service {raw!r}; known services are "
                + ", ".join(KNOWN_SERVICES)
                + ". Omit the path prefix entirely to use LUCIDA_SERVICE"
                f" ({resolved_service()})."
            ),
        )
    return name


def validate_country(service: str, raw: str) -> str:
    """Normalize a country from the path, or reject it.

    Two checks, in order. The shape check exists because the /{service}/{country}
    mount will happily match ANY second path segment, so without it a mistyped
    endpoint like /qobuz/health/ lands in the country slot and is answered with
    a cheerful instance-root payload instead of a 404. lucida's countries are
    ISO alpha-2 codes plus its own XX sentinel, so 2-3 letters covers every
    value it has ever reported.

    The membership check only applies once lucida has actually reported that
    service's accepted set. Before the first search it is empty and anything
    of the right shape passes, because the accepted list is learned from lucida
    rather than guessed - which is the entire point of not hardcoding it.
    """
    code = str(raw).strip().upper()
    if not code.isalpha() or not 2 <= len(code) <= 3:
        raise HTTPException(
            status_code=404,
            detail=(
                f"{raw!r} is not a country code, so it is not a valid second path "
                f"segment. Use /{service} alone, or /{service}/<country> with an "
                "ISO country code (lucida also uses XX as its own sentinel)."
            ),
        )
    accepted = _country_by_service.get(service) or []
    if accepted and code not in accepted:
        raise HTTPException(
            status_code=404,
            detail=(
                f"lucida does not accept country {code!r} for {service}; it "
                "accepts " + ", ".join(accepted[:24])
                + ("..." if len(accepted) > 24 else "")
                + f". {service} is searched with {service_country(service)!r} "
                "when the country is omitted."
            ),
        )
    return code


def resolve_target(request: Request) -> Target:
    """Read the service/country this request is for off its path prefix.

    One handler serves all three mounts, so this is the single place the prefix
    is interpreted. On a bare route both path params are simply absent and the
    request falls back to the configured service.
    """
    raw_service = request.path_params.get("service")
    raw_country = request.path_params.get("country")

    if not raw_service:
        return Target(service=resolved_service(), country="", prefix="", pinned=False)

    service = validate_service(raw_service)
    country = validate_country(service, raw_country) if raw_country else ""
    prefix = f"/{service}" + (f"/{country}" if country else "")
    return Target(service=service, country=country, prefix=prefix, pinned=True)


# ---------------------------------------------------------------------------
# lucida.to wire format
#
# lucida.to has no JSON read API: /search and /?url= are server-rendered
# SvelteKit pages whose machine-readable payload is a JSON5 data node embedded
# in the HTML. Slice it out with these two literal delimiters and parse it as
# JSON5 - the keys are unquoted, so a standard JSON parser fails.
# ---------------------------------------------------------------------------

_PD_START = ',{"type":"data","data":'
_PD_END = ',"uses":{"url":1}}];'


def extract_data_node(html_text: str) -> Optional[Dict[str, Any]]:
    """Return the decoded SvelteKit data node, or None if there isn't one.

    None means the response carried no payload at all - a Cloudflare
    interstitial, typically - which is NOT the same as a payload that decoded
    cleanly to an object reporting a search failure.
    """
    import pyjson5

    start = html_text.find(_PD_START)
    if start < 0:
        return None
    start += len(_PD_START)
    end = html_text.find(_PD_END, start)
    if end < 0:
        return None
    try:
        node = pyjson5.loads(html_text[start:end])
    except Exception:
        logger.warning("lucida data node did not parse as JSON5", exc_info=True)
        return None
    return node if isinstance(node, dict) else None


def accepted_countries(node: Dict[str, Any]) -> List[str]:
    """Country codes lucida accepts for the service this page was rendered for.

    `countries` rides on EVERY search response - success *and* failure - so it
    is readable from a response that just reported `Invalid country for X`.
    That is what makes runtime discovery possible: one deliberately bad request
    is enough to learn the right answer, so the answer cannot go stale.
    """
    envelope = node.get("countries")
    rows = envelope.get("countries") if isinstance(envelope, dict) else envelope
    codes: List[str] = []
    for row in rows or []:
        code = row.get("code") if isinstance(row, dict) else None
        if code and code not in codes:
            codes.append(code)
    return codes


def canonical_album_url(url: str, provider_id: str = "") -> str:
    """Normalise a Qobuz album URL to the form lucida.to resolves most reliably.

    Measured 2026-10-01 against UPC 0886443927087, several attempts each:

      play.qobuz.com/album/{upc}             -> resolves (2/2)
      www.qobuz.com/<locale>/album/<slug>/<upc>  -> resolves, but threw a
                                                 server-side fault once in
                                                 four attempts
      www.qobuz.com/album/<slug>/<upc>       -> "URL not supported" (2/2)
      open.qobuz.com/album/<slug>/<upc>      -> "URL unrecognised" (2/2)

    So lucida wants a locale path segment, or the bare play.* form. Search
    returns the locale-prefixed storefront URL, which works but is not
    dependable, so it is normalised to the canonical form rather than passed
    through and retried. Track URLs need no rewrite: search already returns
    play.qobuz.com/track/{id}, which resolves as-is.
    """
    if not url:
        return ""
    host = (urlparse(url).hostname or "").lower()
    if provider_id and (host == "qobuz.com" or host.endswith(".qobuz.com")):
        return f"https://play.qobuz.com/album/{provider_id}"
    return url


def _artist_names(row: Dict[str, Any]) -> str:
    names: List[str] = []
    for artist in row.get("artists") or []:
        name = (artist.get("name") or "").strip() if isinstance(artist, dict) else ""
        if name and name not in names:
            names.append(name)
    return ", ".join(names)


def _album_from_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = row.get("url")
    if not url or not isinstance(url, str):
        return None
    provider_id = str(row.get("upc") or row.get("id") or "")
    return {
        "url": canonical_album_url(url, provider_id),
        "title": row.get("title") or "",
        "artist": _artist_names(row),
        # Stable across every row referring to this album, so /album/ is
        # reachable from a track's album.id as well as from the album list.
        "key": provider_id or url,
    }


def _track_from_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = row.get("url")
    if not url or not isinstance(url, str):
        return None
    album = row.get("album") if isinstance(row.get("album"), dict) else {}
    album_title = (album.get("title") or "").strip()
    album_key = str(album.get("upc") or album.get("id") or "")
    return {
        "url": url,
        "title": row.get("title") or "",
        "artist": _artist_names(row),
        "album": album_title,
        # The same key /album/ files an album under, so a track's album.id
        # resolves to a record with a usable lucida URL.
        "album_key": album_key or album_title,
    }


def flatten_search(node: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a decoded search data node into tracks/albums/artists lists.

    Owned here rather than delegated to lucidadl, for two reasons. It is what
    tells "this search failed" apart from "this search matched nothing" -
    lucida reports failure in-band at HTTP 200, and the stock client collapses
    both into the same empty result (that is what the vendored patch existed
    for, and it is no longer needed). And it is what carries an album's own id
    through, which is what /album/ needs in order to work at all.
    """
    envelope = node.get("results") if isinstance(node, dict) else None
    if not isinstance(envelope, dict):
        return {"tracks": [], "albums": [], "artists": []}

    out: Dict[str, Any] = {"tracks": [], "albums": [], "artists": []}
    if envelope.get("success") is False:
        # In-band failure at HTTP 200. The nested `results` key is absent here.
        out["error"] = str(envelope.get("error") or "search failed")
        return out

    sets = envelope.get("results") if isinstance(envelope.get("results"), dict) else {}
    for row in sets.get("albums") or []:
        if isinstance(row, dict):
            album = _album_from_row(row)
            if album:
                out["albums"].append(album)
    for row in sets.get("tracks") or []:
        if isinstance(row, dict):
            track = _track_from_row(row)
            if track:
                out["tracks"].append(track)
    for row in sets.get("artists") or []:
        if isinstance(row, dict):
            name = (row.get("name") or "").strip()
            if name:
                out["artists"].append({
                    "url": row.get("url") or "",
                    "name": name,
                    "key": str(row.get("id") or name),
                })
    return out

MEDIA_TYPES = {
    ".flac": "audio/flac",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".wav": "audio/wav",
}

# ---------------------------------------------------------------------------
# Metadata cache — sole owner of the client-facing id space
# ---------------------------------------------------------------------------


class LRUDict(OrderedDict):
    """Simple LRU cache with a hard size cap."""

    def __init__(self, maxsize: int):
        super().__init__()
        self.maxsize = maxsize

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self.maxsize:
            self.popitem(last=False)


class MetadataCache:
    """Holds the track/album/artist records clients refer to by id.

    Clients receive an id from /search/ and hand it back to /info/, /album/,
    /artist/, /trackManifests/, /track/ and the download routes, so every id
    lookup and every record shape is decided here.
    """

    def __init__(self, maxsize: int):
        self.tracks: LRUDict = LRUDict(maxsize)
        self.albums: LRUDict = LRUDict(maxsize)
        self.artists: LRUDict = LRUDict(maxsize)
        # Probe id -> the record it was materialised from, keyed by the service
        # that search ran against. Keyed by service because one global alias
        # would let /soundcloud's capability probe be answered with a Qobuz
        # track, and the probe decides whether a client thinks this instance can
        # download at all.
        self.probes: Dict[str, int] = {}

    @staticmethod
    def id_for(key: str) -> int:
        """Deterministic 64-bit integer id.

        Numeric because clients feed it back through int(); SHA-1 truncated to
        64 bits keeps it collision-free in practice.
        """
        return int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:16], 16)

    def put_track(self, url: str, title: str = "", artist: str = "",
                  album: str = "",
                  album_key: str = "") -> Optional[Dict[str, Any]]:
        """Store a lucida track URL and return its Tidal-shaped record."""
        if not url:
            return None
        artist_name = (artist or "").strip() or "Unknown Artist"
        album_title = (album or "").strip()
        album_id = self.id_for(album_key or album_title) if (album_key or album_title) else None
        artist_id = self.id_for(artist_name)
        record: Dict[str, Any] = {
            "id": self.id_for(url),
            "url": url,
            "title": title or "Unknown",
            "artists": [{"id": artist_id, "name": artist_name}],
            "album": {
                "id": album_id,
                "title": album_title,
            },
            # lucida search exposes no duration or ISRC; 0 means "unknown".
            "duration": 0,
            "audioQuality": "LOSSLESS",
            "explicit": False,
        }
        self.tracks[record["id"]] = record
        self.artists[artist_id] = {"id": artist_id, "name": artist_name}
        if album_id is not None:
            # setdefault, not assignment: an album row from the same search has
            # already filed this album under the SAME id with a real lucida URL,
            # and overwriting that with an empty url is what used to make
            # /album/ report "no tracks" for an album lucida can resolve.
            self.albums.setdefault(album_id, {
                "id": album_id,
                "url": "",
                "key": album_key or album_title,
                "title": album_title,
                "artist": artist_name,
            })
        return record

    def put_album(self, url: str, title: str = "", artist: str = "",
                  key: str = "") -> Optional[Dict[str, Any]]:
        """Store a lucida album URL so /album/ can fetch its track list.

        Filed under `key or url` - the same key `put_track` uses for a track's
        `album.id`. The two used to derive ids differently (album title here,
        album URL there), so a client following /search/ -> track -> album.id
        never found the record this method wrote and /album/ always answered
        with an empty track list.
        """
        if not url:
            return None
        album_id = self.id_for(key or url)
        record = {
            "id": album_id,
            "url": url,
            "key": key or url,
            "title": title or "Unknown Album",
            "artist": artist,
        }
        self.albums[album_id] = record
        return record

    def put_artist(self, name: str) -> Optional[Dict[str, Any]]:
        """Register an artist seen in /search/ under the same id `put_track` uses.

        Keyed by name so an artist reached from a search row and the same artist
        reached from a track's artist field resolve to one record.
        """
        artist_name = (name or "").strip()
        if not artist_name:
            return None
        artist_id = self.id_for(artist_name)
        record = {"id": artist_id, "name": artist_name, "url": ""}
        self.artists[artist_id] = record
        return record

    def alias(self, service: str, record: Dict[str, Any]) -> None:
        """Point the client's probe id at `record`, for `service` only.

        Re-materialised per service rather than cached once: the probe asks
        "can this service download anything", and answering it with another
        service's track would say yes for a source that cannot.
        """
        self.probes[api.normalize_service(service)] = int(record["id"])

    def track(self, raw_id: Optional[str], service: str = "") -> Optional[Dict[str, Any]]:
        """A track record by id, resolving the probe id against `service`."""
        track_id = parse_id(raw_id)
        if track_id is None:
            return None
        if track_id == SOULSYNC_PROBE_ID and service:
            aliased = self.probes.get(api.normalize_service(service))
            if aliased is None:
                return None
            track_id = aliased
        return self.tracks.get(track_id)

    def album(self, raw_id: Optional[str]) -> Optional[Dict[str, Any]]:
        album_id = parse_id(raw_id)
        return self.albums.get(album_id) if album_id is not None else None

    def artist(self, raw_id: Optional[str]) -> Optional[Dict[str, Any]]:
        artist_id = parse_id(raw_id)
        return self.artists.get(artist_id) if artist_id is not None else None


def parse_id(raw: Optional[str]) -> Optional[int]:
    """Client ids arrive as query strings and are integers to us."""
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


cache = MetadataCache(MAX_CACHE_SIZE)

# ---------------------------------------------------------------------------
# lucida.to boundary — every lucida call in the process goes through here
# ---------------------------------------------------------------------------


class LucidaWrapper:
    """Async adapter around lucidadl's LucidaClient.

    Verified against lucidadl 1.4.x. `search` / `resolve_tracks` /
    `download_to_file` are the integration points that may need adjustment.
    """

    def __init__(self) -> None:
        self.client: Optional[api.LucidaClient] = None
        self._init_lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._init_lock:
            if self.client is not None:
                return

            if not await chromium_installed():
                logger.info("Chromium not found, installing...")
                await install_chromium()

            async def _acquire():
                return await acquire_clearance(hidden=True)

            client = api.LucidaClient(
                None,
                USER_AGENT,
                acquire=_acquire,
                country=COUNTRY,
                downscale="original",
                metadata=True,
                jobs=1,
                log=lambda msg: logger.debug("lucidadl: %s", msg),
            )
            await client.start_http()
            self.client = client
            logger.info("lucidadl client initialized (service=%s)", resolved_service())

    async def close(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            try:
                http = getattr(client, "http", None)
                if http is not None:
                    await http.aclose()
            except Exception:
                logger.exception("Error while closing lucidadl HTTP session")

    async def raw_search(self, service: str, country: str,
                         query: str) -> Optional[Dict[str, Any]]:
        """GET lucida's /search page and return its decoded data node.

        Returns None when the response carried no data node at all, which is
        what a Cloudflare challenge looks like - distinct from a node that
        decoded cleanly and then reported a search failure.
        """
        assert self.client is not None
        params = {"service": service, "query": query}
        if country:
            params["country"] = country
        try:
            # _get is private to lucidadl but carries the Cloudflare-refresh
            # and bounded-retry behaviour this proxy depends on; going straight
            # to client.http would drop it.
            response = await self.client._get(api.LUCIDA + "/search", params=params)
        except Exception as exc:
            logger.warning("lucida search request failed for %s: %s", service, exc)
            return None
        return extract_data_node(response.text)

    async def discover_countries(self, service: str) -> List[str]:
        """Learn which countries `service` accepts, from the live service.

        Sent deliberately with a country lucida does not know, because the
        response still carries the accepted set whether the search succeeds or
        fails. Cached per service, so this costs one wasted round-trip per
        service per process - and unlike a hardcoded table it cannot go stale.
        """
        name = api.normalize_service(service)
        async with _country_lock:
            if name in _country_by_service:
                return _country_by_service[name]

        node = await self.raw_search(name, "ZZ", PROBE_QUERY)
        codes = accepted_countries(node) if node else []
        if not codes:
            logger.warning(
                "could not read lucida's accepted countries for %s; using the "
                "bootstrap value %r", name, BOOTSTRAP_COUNTRY.get(name, ""))
            return []

        async with _country_lock:
            _country_by_service[name] = codes
        shown = ", ".join(codes[:8]) + (", ..." if len(codes) > 8 else "")
        logger.info("lucida accepts %d country/countries for %s: %s",
                    len(codes), name, shown)
        stale = BOOTSTRAP_COUNTRY.get(name)
        if stale and stale not in codes:
            logger.warning(
                "bootstrap country %r for %s is not accepted by lucida; "
                "using %r instead", stale, name, codes[0])
        return codes

    async def search(self, query: str, target: Target) -> Dict[str, Any]:
        """Search `target`'s service, failing over when a service is broken upstream.

        A broken service still answers HTTP 200, so a single-service search can
        only ever return "nothing". An empty result from a service that DID
        answer is reported as a plain empty result; an error is reported as an
        error only when no service answered at all.

        A pinned target (one that arrived under a /{service} prefix) searches
        only that service unless LUCIDA_PATH_FALLBACK says otherwise: answering
        a pinned request from a fallback would hand back a different library's
        audio under the name of the one that was asked for, which is the whole
        thing the prefix exists to prevent.
        """
        assert self.client is not None

        configured = target.service
        services = [configured] + (
            [s for s in FALLBACK_SERVICES if s != configured]
            if PATH_FALLBACK or not target.pinned
            else []
        )
        # The pinned country is used as given, even when it is not the one
        # discovery would have picked - it was asked for explicitly. Without
        # one, fall back to whatever lucida says this service accepts.
        country = target.country or service_country(configured)

        # Every failure is kept, not just the last one: reporting only the final
        # error credits whichever fallback hit a wall last, which points at the
        # wrong service whenever the configured one is the problem.
        failures: List[str] = []
        answered = False

        def report(service: str, detail: Any) -> None:
            """Record a failure, at a level matching how much it matters.

            The configured service failing is a real problem worth a warning. A
            fallback failing is routine: fallbacks exist precisely because
            lucida.to services break, and several have stayed broken for good
            (amazon currently 500s on a missing searchMinimal.graphql), so
            warning on them made every healthy search look like a failure.
            """
            failures.append(f"{service}: {detail}")
            log = logger.warning if service == configured else logger.info
            log("lucida search failed on %s: %s", service, detail)

        for service in services:
            # Learn the accepted country first, then search with it. Both are
            # cached, so a service only pays for discovery once. Only the pinned
            # service gets the pinned country; a fallback has to use its own.
            await self.discover_countries(service)
            search_country = country if service == configured else service_country(service)
            node = await self.raw_search(service, search_country, query)
            if node is None:
                report(service, "no data node in response (Cloudflare challenge?)")
                continue
            results = flatten_search(node)
            if results.get("error"):
                report(service, results["error"])
                continue
            answered = True
            if results.get("tracks") or results.get("albums"):
                if service != configured:
                    logger.info("search served by fallback service %s", service)
                return results

        empty: Dict[str, Any] = {"tracks": [], "albums": [], "artists": []}
        if not answered and failures:
            # The one case that genuinely is broken. Logged once, in full, so the
            # cause is never split across per-service lines.
            empty["error"] = "; ".join(failures)
            logger.error(
                "search %r failed on every service: %s", query, empty["error"]
            )
        return empty

    async def probe_search(self, target: Target) -> Dict[str, Any]:
        """The health-probe query, pinned to the same service as the probe caller."""
        return await self.search(PROBE_QUERY, target)

    async def resolve_tracks(self, url: str) -> List[Dict[str, Any]]:
        """Fetch an item page and return its download-available tracks.

        One httpx GET yields the CSRF token (per track), the token expiry and,
        for an album, every track. Tracks whose `producers` is null are not
        available in this country and are skipped.
        """
        assert self.client is not None
        page_data = await self.client.fetch_page_data(url, COUNTRY)
        info = page_data.get("info") or {}
        # A page carrying neither an item URL nor album tracks is a partial or
        # expired response, not an unavailable track. Without this it degrades
        # into "no downloadable track", which reads as a permanent property of
        # the item and hides that a retry would work.
        if not info.get("url") and not info.get("tracks"):
            raise api.LucidaError(f"lucida returned no usable item data for {url}")
        expiry = page_data.get("tokenExpiry")
        resolved: List[Dict[str, Any]] = []
        for track in self.client.tracks_from_pd(page_data):
            if track.get("producers", "x") is None or not track.get("url"):
                continue
            resolved.append({"track": track, "expiry": expiry})
        return resolved

    async def download_to_file(self, url: str, title: str = "",
                               attempts: int = 3) -> Path:
        """Download a lucida.to item URL, retrying transient failures.

        Two failures were observed on live traffic and both cleared on a later
        request: the poll endpoint answering 404 before the server registers
        the request, and an item page briefly coming back unusable. The backoff
        grows because the observed window outlasted a single 3s retry.
        """
        last_error: Optional[Exception] = None
        for attempt in range(max(1, attempts)):
            try:
                return await self._download_once(url, title)
            except Exception as e:
                last_error = e
                if attempt + 1 < attempts:
                    delay = 3 * (attempt + 1)
                    logger.warning(
                        "Download attempt %d/%d failed for %r: %s (retrying in %ds)",
                        attempt + 1, attempts, url, e, delay,
                    )
                    await asyncio.sleep(delay)
        assert last_error is not None
        raise last_error

    async def _download_once(self, url: str, title: str = "") -> Path:
        """One attempt, following lucidadl's own fast HTTP flow:
        resolve the item page, POST /api/load for a handoff, then poll the
        handoff server and stream the finished file."""
        assert self.client is not None

        resolved = await self.resolve_tracks(url)
        if not resolved:
            raise RuntimeError(f"lucida returned no downloadable track for {url}")

        first = resolved[0]
        track = first["track"]
        label = title or track.get("title") or "track"
        handoff, server = await self.client.start_download(
            {
                "url": track["url"],
                "csrf": track.get("csrf"),
                "csrfFallback": track.get("csrfFallback"),
            },
            first["expiry"],
            COUNTRY,
        )
        dest = await self.client.run_job(
            handoff, server, str(DOWNLOAD_DIR), utils.sanitize_filename(label), title=label
        )

        path = Path(dest)
        if not path.exists():
            raise FileNotFoundError(f"lucidadl reported success but file is missing: {path}")
        return path


lucida = LucidaWrapper()


def require_lucida() -> None:
    if lucida.client is None:
        raise HTTPException(status_code=503, detail="Lucida client not initialized")


# ---------------------------------------------------------------------------
# HiFi response shapes
# ---------------------------------------------------------------------------


def hifi(data: Any) -> Dict[str, Any]:
    """Standard {version, data} envelope used by the HiFi API."""
    return {"version": API_VERSION, "data": data}


def legacy_manifest(download_url: str) -> Dict[str, Any]:
    """Base64-JSON legacy manifest for the HiFi /track/ endpoint."""
    payload = json.dumps({"urls": [download_url]}).encode()
    return {
        "version": API_VERSION,
        "data": {
            "assetPresentation": "FULL",
            "audioQuality": "LOSSLESS",
            "manifestMimeType": "application/vnd.tidal.bts",
            "manifest": b64encode(payload).decode(),
        },
    }


def v2_manifest(manifest_url: str) -> Dict[str, Any]:
    """Manifest response for /trackManifests/. Clients read
    `data.data.attributes.uri`, so this nests twice on purpose."""
    return {"data": {"data": {"type": "trackManifests", "attributes": {
        "trackPresentation": "FULL", "uri": manifest_url, "formats": ["FLAC"]}}}}


def hls_manifest(segment_url: str) -> str:
    """Minimal HLS media playlist with ONE segment: the full audio file."""
    target = max(1, int(math.ceil(MANIFEST_DURATION)))
    return (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        f"#EXT-X-TARGETDURATION:{target}\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n"
        f"#EXTINF:{MANIFEST_DURATION:.1f},\n"
        f"{segment_url}\n"
        "#EXT-X-ENDLIST\n"
    )


EMPTY_LEGACY_MANIFEST: Dict[str, Any] = {"version": API_VERSION, "data": {"manifest": ""}}
EMPTY_V2_MANIFEST: Dict[str, Any] = {"data": {"data": {"attributes": {}}}}


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await lucida.initialize()
    except Exception:
        logger.exception("Failed to initialize lucidadl client on startup")
    yield
    await lucida.close()


app = FastAPI(
    title="Lucida.to to HiFi API Proxy (via lucidadl)",
    description="Proxy that maps the lucidadl API to the HiFi API expected by SoulSync",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Every route lives on one router, mounted three times below: bare, under
# /{service}, and under /{service}/{country}. Handlers read their service and
# country off the path via `resolve_target`, so one implementation serves all
# three mounts and cannot drift between them.
router = APIRouter()


@router.get("/")
async def root(target: Target = Depends(resolve_target)):
    """Always 200, so clients probe capabilities instead of treating a
    lazily-initialised backend as an offline instance.

    Echoes the resolved service so a client aimed at /qobuz can confirm it
    landed on the service it meant before it searches for anything."""
    return {
        "version": API_VERSION,
        "status": "online" if lucida.client is not None else "starting",
        "Repo": "local lucida.to proxy",
        "service": target.service,
        "country": target.country or None,
    }


@router.get("/search/")
async def search(
    s: Optional[str] = Query(None, description="Track query"),
    a: Optional[str] = Query(None, description="Artist query"),
    al: Optional[str] = Query(None, description="Album query"),
    v: Optional[str] = Query(None, description="Video query (unsupported)"),
    p: Optional[str] = Query(None, description="Playlist query (unsupported)"),
    i: Optional[str] = Query(None, description="ISRC query (unsupported)"),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=500),
    target: Target = Depends(resolve_target),
):
    """HiFi search. Every documented query field is optional so that clients
    searching by artist or album alone are not answered with a 422."""
    require_lucida()

    # s/a/al are ALTERNATIVE query fields, not one combined search, and real
    # instances use the first one supplied. Combining them asks a far narrower
    # question that collapses a 30-track response to a single track.
    query = next((part.strip() for part in (s, a, al) if part and part.strip()), "")
    if not query:
        return hifi({"limit": limit, "offset": offset, "totalNumberOfItems": 0, "items": []})

    try:
        results = await lucida.search(query, target)
    except Exception as e:
        logger.exception("Search failed for query %r on %s", query, target.service)
        raise HTTPException(status_code=502, detail=f"lucida search failed: {e}")

    # A service-level failure is not "no matches": tell the caller why instead
    # of returning an empty list that looks like a successful empty search.
    if results.get("error") and not (results.get("tracks") or results.get("albums")):
        logger.warning("Search %r unavailable on %s: %s",
                       query, target.service, results["error"])
        raise HTTPException(status_code=502, detail=f"lucida search failed: {results['error']}")

    for album in results.get("albums", []):
        cache.put_album(album.get("url", ""), album.get("title", ""),
                        album.get("artist", ""), album.get("key", ""))

    for artist in results.get("artists", []):
        cache.put_artist(artist.get("name", ""))

    items = [record for record in (
        cache.put_track(track.get("url", ""), track.get("title", ""),
                        track.get("artist", ""), track.get("album", ""),
                        track.get("album_key", ""))
        for track in results.get("tracks", [])
    ) if record is not None]

    return hifi({
        "limit": limit,
        "offset": offset,
        "totalNumberOfItems": len(items),
        "items": items[offset:offset + limit],
    })


@router.get("/info/")
async def info(id: str = Query(...), target: Target = Depends(resolve_target)):
    """Track detail."""
    record = cache.track(id, target.service)
    return hifi(record) if record is not None else hifi({})


@router.get("/album/")
async def album(
    id: str = Query(...),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    target: Target = Depends(resolve_target),
):
    """Album detail with its track list.

    Albums seen in a /search/ response carry a lucida URL and can be resolved;
    the URL is normalised to the canonical form the item route accepts most
    reliably, because search hands back a storefront URL that works but is not
    dependable. An album that was only ever seen as a track's album reference
    has no URL and answers empty.
    """
    require_lucida()

    record = cache.album(id)
    if record is None or not record.get("url"):
        return hifi({
            "id": parse_id(id),
            "title": record.get("title", "") if record else "",
            "items": [],
            "numberOfTracks": 0,
        })

    try:
        resolved = await lucida.resolve_tracks(record["url"])
    except Exception as e:
        logger.exception("Album fetch failed for %r", record["url"])
        raise HTTPException(status_code=502, detail=f"lucida album fetch failed: {e}")

    items = [entry for entry in (
        cache.put_track(entry["track"].get("url", ""), entry["track"].get("title", ""),
                        record.get("artist", ""), record.get("title", ""),
                        record.get("key", ""))
        for entry in resolved
    ) if entry is not None]

    return hifi({
        "id": record["id"],
        "title": record.get("title", ""),
        "artist": {"name": record.get("artist", "")},
        "items": items[offset:offset + limit],
        "numberOfTracks": len(items),
        "duration": 0,
        "releaseDate": "",
    })


@router.get("/artist/")
async def artist(id: str = Query(...)):
    """Artist detail. Resolves artists seen in /search/ results as well as those
    learned from a track's artist field."""
    record = cache.artist(id)
    if record is None:
        return hifi({})
    return hifi({"id": record["id"], "name": record["name"], "url": ""})


# --- playback --------------------------------------------------------------
#
# Client ids resolve through servable_track(): the manifest routes must answer
# without touching lucida (clients probe them on a short timeout), while the
# download routes may search for the health-probe track if it is not cached.


def is_servable(raw_id: str, target: Target) -> bool:
    """Whether an id resolves without a network call.

    The client's health-probe id counts as servable even before it has been
    materialised, because the manifest routes are called on a short timeout and
    must not do a lucida search to answer. Materialising it is the download
    route's job, which is where a network call is already expected.
    """
    if cache.track(raw_id, target.service) is not None:
        return True
    return parse_id(raw_id) == SOULSYNC_PROBE_ID


async def servable_track(raw_id: str, target: Target) -> Optional[Dict[str, Any]]:
    """A client-supplied track id, materialising the probe id on first use.

    The probe search runs against this request's own service, so the id a client
    is handed proves the service it asked about can download, rather than
    proving some other service can.
    """
    record = cache.track(raw_id, target.service)
    if record is not None:
        return record
    if parse_id(raw_id) != SOULSYNC_PROBE_ID or lucida.client is None:
        return None

    # The client probes with a fixed Tidal id before any search has happened,
    # so point it at a real lucida track for the capability check to pass.
    try:
        results = await lucida.probe_search(target)
    except Exception:
        logger.exception("Probe track resolution failed")
        return None
    for track in results.get("tracks", []):
        record = cache.put_track(track.get("url", ""), track.get("title", ""),
                                 track.get("artist", ""), track.get("album", ""))
        if record is not None:
            cache.alias(target.service, record)
            return record
    return None


@router.get("/trackManifests/")
async def track_manifests(
    id: str = Query(...),
    formats: Optional[List[str]] = Query(default=None),
    adaptive: str = Query("true"),
    manifestType: str = Query("MPEG_DASH"),
    uriScheme: str = Query("HTTPS"),
    usage: str = Query("PLAYBACK"),
    target: Target = Depends(resolve_target),
):
    """Only the URI is produced here: the client fetches that playlist and
    streams its segment, which is where the lucida download actually happens.

    The URI keeps the request's own prefix, so a track found under /qobuz
    downloads through /qobuz and not through the bare download route.
    """
    if lucida.client is None or not is_servable(id, target):
        logger.warning("trackManifests: unknown track id %r", id)
        return EMPTY_V2_MANIFEST
    return v2_manifest(f"{target.base_url()}/download/manifest/{parse_id(id)}")


@router.get("/track/")
async def track_legacy(
    id: str = Query(...),
    quality: str = Query("LOSSLESS"),
    target: Target = Depends(resolve_target),
):
    """Legacy fallback for clients that skip HLS. Its base64 manifest carries
    direct audio URLs, so the client fetches the file itself."""
    if lucida.client is None or not is_servable(id, target):
        logger.warning("track: unknown track id %r", id)
        return EMPTY_LEGACY_MANIFEST
    return legacy_manifest(f"{target.base_url()}/download/track/{parse_id(id)}")


@router.get("/download/manifest/{track_key}")
async def download_manifest(track_key: str, target: Target = Depends(resolve_target)):
    """The HLS playlist for a track."""
    require_lucida()

    record = await servable_track(track_key, target)
    if record is None:
        raise HTTPException(status_code=404, detail="Track not found")
    return Response(
        content=hls_manifest(f"{target.base_url()}/download/track/{record['id']}"),
        media_type="application/vnd.apple.mpegurl",
    )


@router.get("/download/track/{track_key}")
async def download_track(track_key: str, target: Target = Depends(resolve_target)):
    """Download the track via lucidadl and stream it back."""
    require_lucida()

    record = await servable_track(track_key, target)
    if record is None:
        raise HTTPException(status_code=404, detail="Track not found")
    url = record.get("url")
    if not url:
        raise HTTPException(status_code=404, detail="Track URL missing")

    # Serve from disk if an earlier request already downloaded this URL.
    file_key = hashlib.md5(url.encode()).hexdigest()
    filepath = next(DOWNLOAD_DIR.glob(f"{file_key}.*"), None)
    if filepath is None:
        try:
            downloaded = await lucida.download_to_file(url, title=record.get("title") or "track")
        except Exception as e:
            logger.exception("Download failed for %r", url)
            raise HTTPException(status_code=502, detail=f"lucida download failed: {e}")
        # Normalize the name to <file_key>.<ext> so repeat requests are free.
        filepath = DOWNLOAD_DIR / f"{file_key}{downloaded.suffix}"
        downloaded.replace(filepath)

    def iterfile(chunk_size: int = 1024 * 512):
        with open(filepath, "rb") as f:
            while chunk := f.read(chunk_size):
                yield chunk

    return StreamingResponse(
        iterfile(),
        media_type=MEDIA_TYPES.get(filepath.suffix.lower(), "application/octet-stream"),
        headers={"Content-Disposition": f'attachment; filename="{filepath.name}"'},
    )


# --- valid-but-empty responses for the rest of the HiFi surface ------------
#
# lucida.to cannot back these (no lyrics, no DRM, no videos, no editorial mixes
# or recommendations). A correctly shaped empty payload keeps a client on this
# instance instead of erroring.


@router.get("/playlist/")
async def playlist_stub(id: str = Query(...), limit: int = Query(100), offset: int = Query(0)):
    return {"version": API_VERSION, "playlist": {"uuid": id, "numberOfTracks": 0}, "items": []}


@router.get("/mix/")
async def mix_stub(id: str = Query(...)):
    return {"version": API_VERSION, "mix": {}, "items": []}


@router.get("/recommendations/")
async def recommendations_stub(id: str = Query(...)):
    return hifi({"limit": 20, "offset": 0, "totalNumberOfItems": 0, "items": []})


@router.get("/cover/")
async def cover_stub(id: Optional[str] = Query(None), q: Optional[str] = Query(None)):
    return {"version": API_VERSION, "covers": []}


@router.get("/lyrics/")
async def lyrics_stub(id: str = Query(...)):
    return {"version": API_VERSION, "lyrics": {}}


@router.get("/artist/similar/")
async def artist_similar_stub(id: str = Query(...)):
    return {"version": API_VERSION, "artists": []}


@router.get("/album/similar/")
async def album_similar_stub(id: str = Query(...)):
    return {"version": API_VERSION, "albums": []}


@router.get("/topvideos/")
async def topvideos_stub():
    return {"version": API_VERSION, "videos": []}


@router.api_route("/widevine", methods=["GET", "POST"])
async def widevine_stub():
    return JSONResponse(
        status_code=501,
        content={"detail": "Widevine DRM is not available through the lucida.to backend"},
    )


@router.get("/playback/requests/{request_id}")
async def playback_request_stub(request_id: str):
    """This proxy downloads inline, so there is never a queued playback job."""
    return JSONResponse(
        status_code=404,
        content={"status": "unknown", "requestId": request_id,
                 "detail": "Playback requests are not used by this proxy"},
    )


# --- mount the router three times ------------------------------------------
#
# Order matters only in that the bare mount goes first, so a request the bare
# routes already claim (/search/, /download/track/{id}) never gets reinterpreted
# as a service prefix. The prefixed mounts are what turn a base URL like
# http://localhost:8002/qobuz into a working SoulSync instance: SoulSync builds
# every call as f"{instance}{path}", so /qobuz/search/ lands here with
# service="qobuz" already parsed out of the path.
#
# include_router does not require the prefix's {service}/{country} params to be
# declared on the handler - FastAPI still puts them in request.path_params,
# which is where resolve_target reads them. That is what lets one handler serve
# all three mounts instead of triplicating every route.
app.include_router(router)
app.include_router(router, prefix="/{service}")
app.include_router(router, prefix="/{service}/{country}")


@app.get("/health")
async def health():
    """Operational health check (not part of the HiFi API).

    Mounted only at the root, not under a prefix: it describes the whole proxy
    rather than one service, so there is nothing for a /{service} variant to
    answer differently.
    """
    if lucida.client is None:
        try:
            await lucida.initialize()
        except Exception as e:
            logger.exception("Lazy initialization in /health failed")
            return {"status": "unhealthy", "lucida_status": "error", "error": str(e)}

    return {
        "status": "healthy",
        "service": "Lucida.to to HiFi Proxy (via lucidadl)",
        "lucida_status": "initialized",
        "cached_tracks": len(cache.tracks),
        "cached_albums": len(cache.albums),
        "backend_service": resolved_service(),
        # False means LUCIDA_SERVICE is not a name this proxy recognizes, so
        # search is being answered by a fallback. Reported here so a typo is one
        # GET /health away instead of buried in a search warning.
        "backend_service_known": resolved_service() in KNOWN_SERVICES,
        "known_services": list(KNOWN_SERVICES),
        "fallback_order": list(FALLBACK_SERVICES),
        # Whether a prefixed request may fall back. False means /qobuz is a hard
        # pin: it either answers from qobuz or reports why it could not.
        "path_fallback": PATH_FALLBACK,
        # The URLs a client can actually paste in as a HiFi instance, each with
        # the service and country it pins, so the list of usable prefixes is
        # never something to derive from this source.
        "mounted_prefixes": (
            [{"prefix": "", "service": resolved_service(),
              "country": service_country(resolved_service()), "pinned": False}]
            + [
                {"prefix": f"/{s}", "service": s, "country": service_country(s),
                 "pinned": True}
                for s in KNOWN_SERVICES
            ]
            + [
                {"prefix": f"/{s}/{c}", "service": s, "country": c, "pinned": True}
                for s in KNOWN_SERVICES
                for c in _country_by_service.get(s, [])[:1]
            ]
        ),
        # Which country each service is searched with, or "" for none, and
        # whether that value came from lucida or from the bootstrap table.
        # A wrong value here is the difference between a working service and
        # an "Invalid country for X" failure, so both are reported.
        "service_country": {s: service_country(s) for s in KNOWN_SERVICES},
        "service_country_source": "lucida" if _country_by_service else "bootstrap",
        # Everything lucida says it accepts, once discovered. Empty until a
        # service has actually been searched.
        "accepted_countries": {
            s: list(_country_by_service.get(s, [])) for s in KNOWN_SERVICES
        },
    }


if __name__ == "__main__":
    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8002"))

    logger.info("Starting Lucida.to -> HiFi proxy on %s:%s", host, port)
    logger.info("API docs: http://%s:%s/docs", host, port)
    logger.info("Clients must reach downloads at PROXY_BASE_URL=%s", PROXY_BASE_URL)
    # Resolved here so an unrecognized service is reported at startup, before the
    # first search, rather than only as a warning once a search has already failed.
    logger.info(
        "Search service: %s (country=%s, refined from lucida on first use), "
        "then fallbacks %s",
        resolved_service(),
        service_country(SERVICE) or "(none)",
        ", ".join(s for s in FALLBACK_SERVICES if s != resolved_service()) or "(none)",
    )
    logger.info("Requires: lucidadl installed, playwright + chromium (auto-installed if missing)")
    logger.info(
        "Per-service URLs: %s/<service> and %s/<service>/<country> (e.g. %s/qobuz)",
        PROXY_BASE_URL, PROXY_BASE_URL, PROXY_BASE_URL,
    )

    uvicorn.run(app, host=host, port=port)
