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
cp .env.example .env      # then set PROXY_BASE_URL (see below)
docker compose up -d      # pulls ghcr.io/blu-tiger/lucida-hifi-proxy:latest
```

Then check it:

```bash
curl http://localhost:8002/health
curl "http://localhost:8002/search/?s=creep&limit=3"
```

There is no build step: compose runs the image published to GHCR, which CI
builds from the `Dockerfile` on every push to `main`. To update, or to build
from a checkout instead:

```bash
docker compose pull && docker compose up -d   # move to the newest published image
docker build -t ghcr.io/blu-tiger/lucida-hifi-proxy:latest .   # or build your own
```

### Setting `PROXY_BASE_URL`

The proxy embeds its own address in the manifest URLs it returns, so this value
must be reachable **from SoulSync**:

| SoulSync runs... | Set `PROXY_BASE_URL` to |
| --- | --- |
| on another machine | the LAN IP of this host, e.g. `http://192.168.1.3:8002` |
| in Docker on this host | `http://host.docker.internal:8002` |

Getting this wrong is the usual cause of "search works but downloads fail".

### Pointing SoulSync at it

Add `http://<host>:8002` as a HiFi API instance in SoulSync's download settings,
and make sure the "HiFi" source is enabled. The proxy answers SoulSync's
`/trackManifests/` capability probe with the health-probe id, so the instance
should show up as able to download.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROXY_BASE_URL` | `http://host.docker.internal:8002` | Address clients use to reach this proxy. |
| `LUCIDA_SERVICE` | `qobuz` | lucida.to service to search. The services lucida.to itself offers are `qobuz`, `tidal`, `soundcloud`, `deezer`, `amazon`, `yandex` and `grilledcheese` (there is no `spotify` — it has been removed). Any other value is tried first but reported as an error at startup and in `/health`. |
| `LUCIDA_COUNTRY` | *(empty = auto)* | Country sent when fetching item pages and starting downloads. Empty sends the literal `auto`, letting lucida pick the account. Search never uses this; see below. |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8002` | Bind address. |
| `LUCIDA_USER_AGENT` | a Chrome UA | User agent used for lucida.to. |
| `LUCIDA_PROBE_QUERY` | `test` | Query used to attach a real track to the client's probe id. |
| `DOWNLOAD_DIR` | `/data/downloads` | Where downloaded audio is cached. Used verbatim when set. |
| `LUCIDADL_HOME` | `/data` | Cloudflare cookie + browser profile location. |`GET /health` reports the resolved `backend_service`, whether
`backend_service_known` is true, the `known_services` list, the `fallback_order`,
the `service_country` in use with its `service_country_source`, and the full
`accepted_countries` lucida reported per service — so a misspelled
`LUCIDA_SERVICE` or a wrong country is one request away instead of buried in a
log.

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
| `qobuz` | `GB` (the only value it accepts) |
| `soundcloud`, `grilledcheese` | `XX` (lucida's own sentinel, labelled `Unknown country`) |
| `amazon` | 48 countries; `US` is one, and omitting the parameter works |
| `tidal`, `deezer` | none configured — every attempt is rejected |
| `yandex`, `spotify` | service disabled server-side |

`/data` is a volume in the compose file, so the Cloudflare clearance, browser
profile and download cache survive restarts. Keeping the clearance is worth it:
re-clearing Cloudflare takes 30–60 seconds.

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
| `GET /health` | Operational health (not part of the HiFi API). |

Everything else in the HiFi surface (`/playlist/`, `/mix/`, `/cover/`,
`/lyrics/`, `/recommendations/`, `/topvideos/`, `/artist/similar/`,
`/album/similar/`) returns a correctly shaped **empty** response rather than an
error, because lucida.to cannot back it. `/playback/requests/{id}` returns 404 —
nothing is ever queued — and `/widevine` returns 501, since there is no DRM.
FastAPI's interactive docs are at `/docs`.

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
  repeatedly, `amazon` fails every search on a backend `ENOENT`,
  `yandex` reports itself disabled, and `tidal`/`deezer` reject every country we
  could find. `grilledcheese` and `soundcloud` answered throughout. Because this
  moves, the proxy tries `LUCIDA_SERVICE`, then falls back, and returns
  **502 naming every service that failed** only when *no* service answers — a
  query that genuinely matched nothing still returns 200 with zero items.

  A failing fallback logs at `INFO` and a failing *configured* service logs at
  `WARNING`; only a search no service answered logs at `ERROR`. So a healthy
  search is quiet, and a `WARNING ... failed on qobuz` line is the one to read.
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
