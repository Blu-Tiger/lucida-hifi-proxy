# lucida-hifi-proxy

A small HTTP proxy that exposes the [HiFi API](https://github.com/binimum/hifi-api)
surface — the one [SoulSync](https://github.com/Nezreka/SoulSync) speaks for its
"HiFi" download source — but serves the audio from
lucida.to instead of Tidal. No Tidal account required.

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
| `LUCIDA_COUNTRY` | `US` | Country used when fetching item pages and starting downloads. Search does **not** use this: each service accepts only specific countries, so search uses a per-service table (see below). |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8002` | Bind address. |
| `LUCIDA_USER_AGENT` | a Chrome UA | User agent used for lucida.to. |
| `LUCIDA_PROBE_QUERY` | `test` | Query used to attach a real track to the client's probe id. |
| `DOWNLOAD_DIR` | `/data/downloads` | Where downloaded audio is cached. Used verbatim when set. |
| `LUCIDADL_HOME` | `/data` | Cloudflare cookie + browser profile location. |

`GET /health` reports the resolved `backend_service`, whether
`backend_service_known` is true, the `known_services` list, the
`fallback_order`, and the `service_country` table — so a misspelled
`LUCIDA_SERVICE` or a wrong country is one request away instead of buried in a
log.

### Search countries are per service

lucida.to rejects any country a service does not accept with a bare
`Invalid country for X`, and the accepted set differs per service. Sending one
country for everything silently disables most services:

| Service | Country sent |
| --- | --- |
| `qobuz` | `US` (the only value it accepts) |
| `soundcloud`, `grilledcheese` | `XX` (the only value they accept) |
| `amazon` | `US` (it accepts 48 countries) |
| `tidal`, `deezer`, `yandex` | none found that works |

lucidadl ships the wrong table — it sends `US` for every service it does not
know, which makes SoundCloud and GrilledCheese fail every single search. The
proxy corrects this at import time (`SERVICE_COUNTRY`), so the fix survives
upgrading lucidadl rather than living in the vendored copy.

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
| `GET /album/` | Album detail. **Currently always returns an empty track list — see below.** |
| `GET /artist/` | Artist name for an artist id seen in search results. |
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

- **`/album/` can never list tracks.** Search returns tracks only, and a track's
  album id is derived from the album *title*, so the album has no lucida URL to
  fetch. The route returns a valid but empty track list.
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

One caveat worth knowing: this repo carries a **single local patch** inside the
vendored `lucidadl/api.py`, which is how the proxy detects a failed search
instead of silently returning no results. If you update `lucidadl/` from
upstream, re-apply it — see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for
the details. The vendored copy is also **pruned to the modules the proxy
actually imports**, so it cannot stand in for the upstream CLI.
