# lucida-hifi-proxy

A small HTTP proxy that exposes the [HiFi API](https://github.com/binimum/hifi-api)
surface — the one [SoulSync](https://github.com/Nezreka/SoulSync) speaks for its
"HiFi" download source — but serves the audio from
lucida.to.

Point SoulSync's HiFi source at this proxy instead of a public HiFi instance.

## ⚠️ This is vibecoded

This project was written by an AI coding agent running **DeepSeek V4.1 Flash**,
working from a human's instructions and reviewed by that human. It is not the work of someone maintaining a library for
a living. Treat it accordingly:

- It talks to an undocumented, unversioned third-party service (lucida.to) and
  **will break when that service changes.**
- Compatibility is with one client (SoulSync), verified by reading that client's
  source — not against a specification.
- See [Known limitations](#known-limitations) for things that are knowingly
  broken or unproven.

Read the code before trusting it with anything you care about.

## Quick start

```bash
git clone https://github.com/Blu-Tiger/lucida-hifi-proxy.git
cd lucida-hifi-proxy
cp .env.example .env      # every setting is optional; the defaults work as-is
docker compose up -d      # pulls ghcr.io/blu-tiger/lucida-hifi-proxy:latest
```

Then check it:

```bash
curl http://localhost:8002/health
curl "http://localhost:8002/search/?s=creep&limit=3"

# Pin one service (what you'd paste into SoulSync):
curl "http://localhost:8002/qobuz/search/?s=creep&limit=3"
curl "http://localhost:8002/qobuz/GB/search/?s=creep&limit=3"
```

There is no build step: compose runs the image published to GHCR, which CI
builds from the `Dockerfile` on every push to `main`. To update, or to build
from a checkout instead:

```bash
docker compose pull && docker compose up -d   # move to the newest published image
docker build -t ghcr.io/blu-tiger/lucida-hifi-proxy:latest .   # or build your own
```

### `PROXY_BASE_URL` is usually best left empty

The proxy embeds its own address in the manifest URLs it returns, and takes
that address from the **Host header of the request itself** unless
`PROXY_BASE_URL` overrides it. A client can always reach the address it just
used, so the default needs no configuration and cannot disagree with how
SoulSync actually reaches the proxy.

An earlier revision pinned `http://host.docker.internal:8002` instead, which
broke downloads *silently* whenever the client could not resolve it: the
manifest was answered, and the download request it pointed at simply never
arrived. Set the variable only when the Host header is not what clients should
use (a reverse proxy, for instance), and give the address clients must reach:

| Situation | `PROXY_BASE_URL` |
| --- | --- |
| Default - any normal setup | leave empty |
| Behind a reverse proxy | the public URL, e.g. `https://lucida.example.com` |

If it is set and disagrees with an incoming request's Host, the proxy logs one
warning naming both addresses.

### Pointing SoulSync at it

Add the proxy as a HiFi API instance in SoulSync's download settings, and make
sure the "HiFi" source is enabled. The proxy answers SoulSync's
`/trackManifests/` capability probe with the health-probe id, so the instance
should show up as able to download.

Any of these work as the instance URL:

| Instance URL | Searches | Notes |
| --- | --- | --- |
| `http://<host>:8002` | `LUCIDA_SERVICE`, then the fallback chain | The default; nothing pinned |
| `http://<host>:8002/qobuz` | qobuz only | Hard pin — see below |
| `http://<host>:8002/qobuz/GB` | qobuz only, searched with `GB` | Country omitted = lucida's own choice |

So pinning a service is just adding it to the URL. **Add several** if you want
SoulSync's failover between them — `http://<host>:8002/qobuz` then
`http://<host>:8002/grilledcheese` gives a lossless-first, lossless-second chain
without any fallback code involved.

### Per-service URLs

Everything is mounted three times — bare, under `/<service>`, and under
`/<service>/<country>` — so any of those URLs is a complete, working instance.
That works because SoulSync builds every call as `f"{instance}{path}"` and
never parses the URL, so `/qobuz/search/` arrives at the proxy with `qobuz`
already readable off the path.

Three behaviours are worth knowing before you paste one in:

- **A prefix is a hard pin.** `/qobuz` searches qobuz and *only* qobuz. If qobuz
  is broken upstream the request fails with a `502` naming qobuz, rather than
  quietly answering from another service with different audio. Set
  `LUCIDA_PATH_FALLBACK=1` to opt back into the fallback chain. The bare URL is
  unaffected and keeps falling back as it always has.
- **A bad service is a `404`, not a silent fallback.** `/qobuzz` names the
  services that exist rather than returning plausible results from somewhere
  else. A country lucida has rejected for that service gets the same treatment,
  with the accepted list in the message.
- **The pin survives to the download.** Manifest and audio URLs keep the prefix,
  so a track found under `/qobuz` downloads through `/qobuz`.

**The country segment pins *search* only.** lucida's item route (`GET /?url=`)
was measured ignoring `country` outright — `GB`, `US` and `FR` all resolved
against the same `GB00` account — so `/qobuz/GB` chooses which catalogue the
search runs against, not which account the download uses. That route takes
`LUCIDA_COUNTRY`, which is empty by design.

`GET /health` reports every usable prefix under `mounted_prefixes`, each with
the service and country it pins, so the list is never something to derive from
this README.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROXY_BASE_URL` | *(empty = auto)* | Overrides the address embedded in manifest URLs. Empty uses the Host header of each request, which is by definition reachable from the client that made it. |
| `LUCIDA_SERVICE` | `qobuz` | lucida.to service to search. The services lucida.to itself offers are `qobuz`, `tidal`, `soundcloud`, `deezer`, `amazon`, `yandex` and `grilledcheese` (there is no `spotify` — it has been removed). Any other value is tried first but reported as an error at startup and in `/health`. |
| `LUCIDA_COUNTRY` | *(empty = auto)* | Country sent when fetching item pages and starting downloads. Empty sends the literal `auto`, letting lucida pick the account. Search never uses this; see below. |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8002` | Bind address. |
| `LUCIDA_USER_AGENT` | a Chrome UA | User agent used for lucida.to. |
| `LUCIDA_PROBE_QUERY` | `test` | Query used to attach a real track to the client's probe id. |
| `LUCIDA_PATH_FALLBACK` | *(off)* | Set to `1` to let a `/{service}` prefix fall back to another service. Off by default: a prefix is an explicit pin. The bare URL always falls back. |
| `LUCIDA_CLEARANCE_COOLDOWN` | `60` | Seconds to wait before launching the browser again after a failed Cloudflare-clearance attempt, so a browser that cannot start is not relaunched on every challenged request. |
| `DOWNLOAD_DIR` | `/data/downloads` | Where downloaded audio is cached. Used verbatim when set. |
| `LUCIDADL_HOME` | `/data` | Cloudflare cookie + browser profile location. |`GET /health` reports the resolved `backend_service`, whether
`backend_service_known` is true, the `known_services` list, the `fallback_order`,
whether `path_fallback` is on, the `mounted_prefixes` you can paste into a
client, the `service_country` in use with its `service_country_source`, and the
full `accepted_countries` lucida reported per service — so a misspelled
`LUCIDA_SERVICE` or a wrong country is one request away instead of buried in a
log. It is mounted only at the root, since it describes the whole proxy rather
than one service.

### Search countries are discovered, not hardcoded

lucida.to rejects any country a service does not accept with a bare
`Invalid country for X`, and the accepted set differs per service. Sending one
country for everything silently disables most services.

**This proxy now asks lucida instead of guessing.** Every `/search` response —
including one that reports `Invalid country for X` — carries a `countries` member
listing what that service accepts. So on first use of a service the proxy sends
one deliberately-bad country, reads the accepted set out of the reply, caches it,
and searches with the right value from then on. `GET /health` reports both the
value in use and `service_country_source` (`lucida` once measured, `bootstrap`
before).

`BOOTSTRAP_COUNTRY` remains only as a fallback for when that probe cannot run.
It is a safety net, not the source of truth.

> **Why this changed:** an earlier revision shipped a hardcoded table with
> `qobuz -> US`, and qobuz accepts **GB only**. With `LUCIDA_SERVICE=qobuz` as the
> default, every search failed with `Invalid country for qobuz`, logged one
> `WARNING`, and was quietly answered by the fallback chain — so the proxy looked
> healthy while never using qobuz at all. The lesson generalises: lucida publishes
> the answer on every response, and hardcoding it is how it goes stale.

Last measured from lucida's own `<select id="country">` (2026-10-01):

| Service | Country |
| --- | --- |
| `qobuz` | `GB` on 2026-10-01, `NL` on 2026-10-02 — the only value each time |
| `soundcloud`, `grilledcheese` | `XX` (lucida's own sentinel, labelled `Unknown country`) |
| `amazon` | 48 countries; `US` is one, and omitting the parameter works |
| `tidal`, `deezer` | none configured — every attempt is rejected |
| `yandex`, `spotify` | service disabled server-side |

`/data` is a volume in the compose file, so the Cloudflare clearance, browser
profile and download cache survive restarts. Keeping the clearance is worth it:
re-clearing Cloudflare takes 30–60 seconds, and the proxy loads the saved
cookie at startup before it touches lucida.

The two `/data` defaults above come from the image. Running
`python lucida_hifi_proxy.py` directly falls back differently:
`DOWNLOAD_DIR` to `$TMPDIR/lucida_proxy` (`%TEMP%\lucida_proxy` on Windows), and
`LUCIDADL_HOME` to lucidadl's own per-user data directory
(`~/.local/share/lucidadl`, or `%LOCALAPPDATA%\lucidadl` on Windows).

## Endpoints

| Endpoint | Behaviour |
| --- | --- |
| `GET /` | Version + status. Always 200. |
| `GET /search/` | Search. `s`, `a`, `al` are alternative query fields; the first one supplied is used. |
| `GET /info/` | Track detail for an id from `/search/`. |
| `GET /album/` | Album detail with its track list. Works for albums seen in `/search/`. |
| `GET /artist/` | Artist name for an artist id seen in search results, or in a track's artist field. |
| `GET /trackManifests/` | HLS playlist URI (one segment: the whole file). |
| `GET /track/` | Legacy base64 manifest with direct audio URLs. |
| `GET /download/manifest/{id}` | The URL embedded in the HLS playlist. |
| `GET /download/track/{id}` | The URL embedded in the legacy manifest; serves the audio. |
| `GET /health` | Operational health (not part of the HiFi API). Root only. |

Every route above except `/health` also answers under `/<service>` and
`/<service>/<country>`.

Everything else in the HiFi surface (`/playlist/`, `/mix/`, `/cover/`,
`/lyrics/`, `/recommendations/`, `/topvideos/`, `/artist/similar/`,
`/album/similar/`) returns a correctly shaped **empty** response rather than an
error, because lucida.to cannot back it. `/playback/requests/{id}` returns 404 —
nothing is ever queued — and `/widevine` returns 501, since there is no DRM.
FastAPI's interactive docs are at `/docs`.

## Troubleshooting

### Every search returns 502 and the logs show HTTP 403 from lucida.to

That is Cloudflare, not lucida and not one service. A request with no valid
`cf_clearance` cookie gets a *managed challenge* (HTTP 403 with
`cf-mitigated: challenge`) for every service at once, which is why the error
names each service with the same "no data node in response".

The proxy handles it in three layers: it reuses the `cf_clearance` cookie
lucidadl saved under `LUCIDADL_HOME` (`/data/clearance.json` in the container,
which is on a volume); it solves the challenge with a browser when no cookie is
saved or the cookie stops working; and it reports the failure at `WARNING` with
the actual reason when neither works. Look for `Cloudflare clearance acquired`
or `Cloudflare refresh failed: ...` in the logs, and check
`GET /health` → `cloudflare` (`clearance: true`, plus the last `error`).

If the refresh keeps failing in a container, the image's browser or its X
display is the problem - the published image bundles both. `docker compose
pull && docker compose up -d` moves to the current image; the clearance that
solves the challenge then lives in the `lucida-data` volume and is reused on
later restarts.

### Search works but downloads never start

SoulSync could not fetch the URL the manifest pointed at. Check that
`PROXY_BASE_URL` is unset (see above) - a value the client cannot reach
produces exactly this symptom, and it is invisible from the proxy because the
download request never arrives.

## Known limitations

These are real and reproduce; they are not hypothetical.

- **Albums are resolved through a rewritten URL.** Search returns Qobuz storefront
  album URLs (`www.qobuz.com/<locale>/album/<slug>/<upc>`). lucida accepts those, but
  threw a server-side fault on 1 of 4 attempts, and refuses the locale-less
  storefront path (`URL not supported`) and the `open.` host (`URL unrecognised`)
  outright. The proxy normalises to `play.qobuz.com/album/{id}`, which resolved on
  every attempt. `/album/` answers with an empty track list only for an album that
  was never seen as an album row in `/search/` and so has no URL at all.
- **Search depends on lucida.to's own service health, which changes.** Measured
  in one session: `qobuz` broke mid-session with an upstream 403 after working
  repeatedly (the 403s turned out to be Cloudflare challenges - see
  Troubleshooting), `amazon` failed every search on a backend `ENOENT` one day
  and answered normally the next,
  `yandex` reports itself disabled, and `tidal`/`deezer` reject every country we
  could find. `grilledcheese` and `soundcloud` answered throughout. Because this
  moves, the proxy tries `LUCIDA_SERVICE`, then falls back, and returns
  **502 naming every service that failed** only when *no* service answers — a
  query that genuinely matched nothing still returns 200 with zero items.

  A failing fallback logs at `INFO` and a failing *configured* service logs at
  `WARNING`; only a search no service answered logs at `ERROR`. So a healthy
  search is quiet, and a `WARNING ... failed on qobuz` line is the one to read.

  A `/{service}` prefix opts out of all of that: it searches that one service and
  reports the failure. That is the point of a pin, but it does mean an outage on
  a pinned service becomes a visible `502` instead of a quiet substitution.
- **Falling back can change what you get.** Fallbacks are ordered lossless
  first, but they are different libraries: `soundcloud` returns lossy audio and
  puts the *uploader* in the artist field (`Radiohead - Creep` / `deathismercy`),
  where `qobuz` and `grilledcheese` return FLAC. If a result looks wrong or
  sounds lossy, the search was served by a fallback. `grilledcheese` is an
  official lucida.to service, but it is served by a third party whose catalogue
  and reliability are not yours to control.
- **Some queries make lucida.to error out** server-side
  (`Cannot read properties of undefined (reading 'name')`) on every service.
  Those searches return 502.
- **The download cache never expires.** `DOWNLOAD_DIR` grows without bound; clear
  it occasionally.
- **Lossless playback relies on the client's own demuxing.** SoulSync demuxes
  FLAC out of an MP4 container for lossless tiers. The proxy serves native FLAC,
  where that is a pass-through, but this combination has not been exercised
  end-to-end.
- **A container needs a virtual display.** lucida.to's Cloudflare check rejects a
  truly headless browser, so the image runs Chromium under Xvfb. Expect it to use
  a few hundred MB of RAM.
- **The image is published under `:latest` with no versioned tags yet**, so a
  fresh pull can change behaviour under you. Pushing a `v*` tag makes CI publish
  a version tag you can pin to instead.

## Legal

Personal, educational use. Downloading copyrighted music is illegal in most
countries, and using this may put your IP at risk with the services involved.
Do not expose this proxy to the internet. Not affiliated with lucida.to, Tidal,
HiFi API or SoulSync.

## Credits

- [binimum/hifi-api](https://github.com/binimum/hifi-api) (MIT) — the HTTP
  surface and response shapes this proxy imitates.
- [Nezreka/SoulSync](https://github.com/Nezreka/SoulSync) — the client whose
  expectations defined what "compatible" means here.
- [Jude-A/lucidadl](https://github.com/Jude-A/lucidadl) (MIT) — the vendored
downloader that does the actual lucida.to work.
- Written by an AI coding agent running **DeepSeek V4.1 Flash** under human
  direction and review — see [This is vibecoded](#this-is-vibecoded) above.

## License

MIT — see [LICENSE](LICENSE) for this project's own code, and
[lucidadl/LICENSE](lucidadl/LICENSE) for the vendored `lucidadl/` package, which
is also MIT (Copyright (c) 2026 Jude-A and lucidadl contributors).

One caveat worth knowing: the vendored `lucidadl/` used to carry a **single local
patch** inside `api.py`, which is how the proxy detected a failed search instead
of silently returning no results. The proxy no longer needs it — it issues and
parses the search request itself — so **the patch has been removed and the
vendored copy is now unmodified upstream**. Updating `lucidadl/` is a clean file
copy with nothing to re-apply. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The vendored copy is also
**pruned to the modules the proxy actually imports**, so it cannot stand in for
the upstream CLI.
