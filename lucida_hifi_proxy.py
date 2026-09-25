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

Ownership: `MetadataCache` owns the client-facing id space, `LucidaWrapper`
owns every lucida.to call, and the routes own the HTTP contract (status codes,
response shapes, and the error/no-match policy).
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
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Response
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
# the accepted set differs per service - it is NOT one global country. Measured
# 2026-09 against lucida.to's own <select id="country">:
#   qobuz     US only
#   amazon    48 countries (US is one); its search backend is broken separately
#   soundcloud, grilledcheese   XX only
#   tidal, deezer, yandex      no country we could find that works
SERVICE_COUNTRY = {
    "qobuz": "US",
    "soundcloud": "XX",
    "grilledcheese": "XX",
    "amazon": "US",
}

# Fallback order for a HiFi proxy, so lossless sources are preferred and lossy
# ones are a last resort: qobuz and grilledcheese both returned verified FLAC,
# while soundcloud serves lossy audio and reports the uploader as the artist.
# amazon (backend ENOENT), yandex ("currently disabled") and tidal/deezer are
# left out because each was measured to fail every search, and retrying a dead
# service costs a wasted upstream round-trip on every search. Set LUCIDA_SERVICE
# to one of them explicitly to use it anyway.
FALLBACK_SERVICES = ("qobuz", "grilledcheese", "soundcloud")
COUNTRY = os.getenv("LUCIDA_COUNTRY", "US")
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
    """Country to search `service` with, or "" to send no country param."""
    return SERVICE_COUNTRY.get(api.normalize_service(service), "")


# lucidadl answers "which country?" from a module-level function that
# `LucidaClient.search` calls directly, and its shipped answers are wrong: it
# sends US for every service it does not know, but SoundCloud and GrilledCheese
# accept ONLY XX, so they failed outright with "Invalid country" and read as
# permanently broken services. Overriding it here rather than editing the
# vendored copy keeps the correction attached to this proxy, where it survives
# reinstalling or upgrading lucidadl. Verified live: soundcloud and
# grilledcheese both return tracks through this path.
api.default_country = service_country

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

    @staticmethod
    def id_for(key: str) -> int:
        """Deterministic 64-bit integer id.

        Numeric because clients feed it back through int(); SHA-1 truncated to
        64 bits keeps it collision-free in practice.
        """
        return int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:16], 16)

    def put_track(self, url: str, title: str = "", artist: str = "",
                  album: str = "") -> Optional[Dict[str, Any]]:
        """Store a lucida track URL and return its Tidal-shaped record."""
        if not url:
            return None
        artist_name = (artist or "").strip() or "Unknown Artist"
        album_title = (album or "").strip()
        artist_id = self.id_for(artist_name)
        record: Dict[str, Any] = {
            "id": self.id_for(url),
            "url": url,
            "title": title or "Unknown",
            "artists": [{"id": artist_id, "name": artist_name}],
            "album": {
                "id": self.id_for(album_title) if album_title else None,
                "title": album_title,
            },
            # lucida search exposes no duration or ISRC; 0 means "unknown".
            "duration": 0,
            "audioQuality": "LOSSLESS",
            "explicit": False,
        }
        self.tracks[record["id"]] = record
        self.artists[artist_id] = {"id": artist_id, "name": artist_name}
        if album_title:
            self.albums.setdefault(self.id_for(album_title), {
                "id": self.id_for(album_title),
                "url": "",
                "title": album_title,
                "artist": artist_name,
            })
        return record

    def put_album(self, url: str, title: str = "",
                  artist: str = "") -> Optional[Dict[str, Any]]:
        """Store a lucida album URL so /album/ can fetch its track list."""
        if not url:
            return None
        record = {
            "id": self.id_for(url),
            "url": url,
            "title": title or "Unknown Album",
            "artist": artist,
        }
        self.albums[record["id"]] = record
        return record

    def alias(self, alias_id: int, record: Dict[str, Any]) -> None:
        """Serve an existing record under an extra id (the client's probe id)."""
        self.tracks[alias_id] = record

    def track(self, raw_id: Optional[str]) -> Optional[Dict[str, Any]]:
        track_id = parse_id(raw_id)
        return self.tracks.get(track_id) if track_id is not None else None

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

    async def search(self, query: str) -> Dict[str, Any]:
        """Search, failing over when a service is broken upstream.

        A broken service still answers HTTP 200, so a single-service search can
        only ever return "nothing". An empty result from a service that DID
        answer is reported as a plain empty result; an error is reported as an
        error only when no service answered at all.
        """
        assert self.client is not None

        configured = resolved_service()
        services = [configured] + [s for s in FALLBACK_SERVICES if s != configured]

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
            try:
                results = await self.client.search(query=query, service=service)
            except Exception as e:
                report(service, e)
                continue
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


@app.get("/")
async def root():
    """Always 200, so clients probe capabilities instead of treating a
    lazily-initialised backend as an offline instance."""
    return {
        "version": API_VERSION,
        "status": "online" if lucida.client is not None else "starting",
        "Repo": "local lucida.to proxy",
    }


@app.get("/search/")
async def search(
    s: Optional[str] = Query(None, description="Track query"),
    a: Optional[str] = Query(None, description="Artist query"),
    al: Optional[str] = Query(None, description="Album query"),
    v: Optional[str] = Query(None, description="Video query (unsupported)"),
    p: Optional[str] = Query(None, description="Playlist query (unsupported)"),
    i: Optional[str] = Query(None, description="ISRC query (unsupported)"),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=500),
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
        results = await lucida.search(query)
    except Exception as e:
        logger.exception("Search failed for query %r", query)
        raise HTTPException(status_code=502, detail=f"lucida search failed: {e}")

    # A service-level failure is not "no matches": tell the caller why instead
    # of returning an empty list that looks like a successful empty search.
    if results.get("error") and not (results.get("tracks") or results.get("albums")):
        logger.warning("Search %r unavailable: %s", query, results["error"])
        raise HTTPException(status_code=502, detail=f"lucida search failed: {results['error']}")

    for album in results.get("albums", []):
        cache.put_album(album.get("url", ""), album.get("title", ""), album.get("artist", ""))

    items = [record for record in (
        cache.put_track(track.get("url", ""), track.get("title", ""),
                        track.get("artist", ""), track.get("album", ""))
        for track in results.get("tracks", [])
    ) if record is not None]

    return hifi({
        "limit": limit,
        "offset": offset,
        "totalNumberOfItems": len(items),
        "items": items[offset:offset + limit],
    })


@app.get("/info/")
async def info(id: str = Query(...)):
    """Track detail."""
    record = cache.track(id)
    return hifi(record) if record is not None else hifi({})


@app.get("/album/")
async def album(
    id: str = Query(...),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Album detail with its track list. Only albums that came from /search/
    (and so have a lucida URL) can be fetched."""
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
                        record.get("artist", ""), record.get("title", ""))
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


@app.get("/artist/")
async def artist(id: str = Query(...)):
    """Artist detail. Lucida search does not return artists, so this resolves
    only artists learned from track results."""
    record = cache.artist(id)
    if record is None:
        return hifi({})
    return hifi({"id": record["id"], "name": record["name"], "url": ""})


# --- playback --------------------------------------------------------------
#
# Client ids resolve through servable_track(): the manifest routes must answer
# without touching lucida (clients probe them on a short timeout), while the
# download routes may search for the health-probe track if it is not cached.


def is_servable(raw_id: str) -> bool:
    """Whether an id resolves without a network call: a cached track, or the
    client's health-probe id, which downloads lazily."""
    track_id = parse_id(raw_id)
    return track_id is not None and (
        track_id in cache.tracks or track_id == SOULSYNC_PROBE_ID
    )


async def servable_track(raw_id: str) -> Optional[Dict[str, Any]]:
    """A client-supplied track id, materialising the probe id on first use."""
    record = cache.track(raw_id)
    if record is not None:
        return record
    if parse_id(raw_id) != SOULSYNC_PROBE_ID or lucida.client is None:
        return None

    # The client probes with a fixed Tidal id before any search has happened,
    # so point it at a real lucida track for the capability check to pass.
    try:
        results = await lucida.search(PROBE_QUERY)
    except Exception:
        logger.exception("Probe track resolution failed")
        return None
    for track in results.get("tracks", []):
        record = cache.put_track(track.get("url", ""), track.get("title", ""),
                                 track.get("artist", ""), track.get("album", ""))
        if record is not None:
            cache.alias(SOULSYNC_PROBE_ID, record)
            return record
    return None


@app.get("/trackManifests/")
async def track_manifests(
    id: str = Query(...),
    formats: Optional[List[str]] = Query(default=None),
    adaptive: str = Query("true"),
    manifestType: str = Query("MPEG_DASH"),
    uriScheme: str = Query("HTTPS"),
    usage: str = Query("PLAYBACK"),
):
    """Only the URI is produced here: the client fetches that playlist and
    streams its segment, which is where the lucida download actually happens."""
    if lucida.client is None or not is_servable(id):
        logger.warning("trackManifests: unknown track id %r", id)
        return EMPTY_V2_MANIFEST
    return v2_manifest(f"{PROXY_BASE_URL}/download/manifest/{parse_id(id)}")


@app.get("/track/")
async def track_legacy(id: str = Query(...), quality: str = Query("LOSSLESS")):
    """Legacy fallback for clients that skip HLS. Its base64 manifest carries
    direct audio URLs, so the client fetches the file itself."""
    if lucida.client is None or not is_servable(id):
        logger.warning("track: unknown track id %r", id)
        return EMPTY_LEGACY_MANIFEST
    return legacy_manifest(f"{PROXY_BASE_URL}/download/track/{parse_id(id)}")


@app.get("/download/manifest/{track_key}")
async def download_manifest(track_key: str):
    """The HLS playlist for a track."""
    require_lucida()

    record = await servable_track(track_key)
    if record is None:
        raise HTTPException(status_code=404, detail="Track not found")
    return Response(
        content=hls_manifest(f"{PROXY_BASE_URL}/download/track/{record['id']}"),
        media_type="application/vnd.apple.mpegurl",
    )


@app.get("/download/track/{track_key}")
async def download_track(track_key: str):
    """Download the track via lucidadl and stream it back."""
    require_lucida()

    record = await servable_track(track_key)
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


@app.get("/playlist/")
async def playlist_stub(id: str = Query(...), limit: int = Query(100), offset: int = Query(0)):
    return {"version": API_VERSION, "playlist": {"uuid": id, "numberOfTracks": 0}, "items": []}


@app.get("/mix/")
async def mix_stub(id: str = Query(...)):
    return {"version": API_VERSION, "mix": {}, "items": []}


@app.get("/recommendations/")
async def recommendations_stub(id: str = Query(...)):
    return hifi({"limit": 20, "offset": 0, "totalNumberOfItems": 0, "items": []})


@app.get("/cover/")
async def cover_stub(id: Optional[str] = Query(None), q: Optional[str] = Query(None)):
    return {"version": API_VERSION, "covers": []}


@app.get("/lyrics/")
async def lyrics_stub(id: str = Query(...)):
    return {"version": API_VERSION, "lyrics": {}}


@app.get("/artist/similar/")
async def artist_similar_stub(id: str = Query(...)):
    return {"version": API_VERSION, "artists": []}


@app.get("/album/similar/")
async def album_similar_stub(id: str = Query(...)):
    return {"version": API_VERSION, "albums": []}


@app.get("/topvideos/")
async def topvideos_stub():
    return {"version": API_VERSION, "videos": []}


@app.api_route("/widevine", methods=["GET", "POST"])
async def widevine_stub():
    return JSONResponse(
        status_code=501,
        content={"detail": "Widevine DRM is not available through the lucida.to backend"},
    )


@app.get("/playback/requests/{request_id}")
async def playback_request_stub(request_id: str):
    """This proxy downloads inline, so there is never a queued playback job."""
    return JSONResponse(
        status_code=404,
        content={"status": "unknown", "requestId": request_id,
                 "detail": "Playback requests are not used by this proxy"},
    )


@app.get("/health")
async def health():
    """Operational health check (not part of the HiFi API)."""
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
        # Which country each service is searched with, or "" for none. Wrong
        # values here are the difference between a working service and an
        # "Invalid country for X" failure.
        "service_country": {s: service_country(s) for s in KNOWN_SERVICES},
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
        "Search service: %s (country=%s), then fallbacks %s",
        resolved_service(),
        service_country(SERVICE) or "(none)",
        ", ".join(s for s in FALLBACK_SERVICES if s != resolved_service()) or "(none)",
    )
    logger.info("Requires: lucidadl installed, playwright + chromium (auto-installed if missing)")

    uvicorn.run(app, host=host, port=port)
