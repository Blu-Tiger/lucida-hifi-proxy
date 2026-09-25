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
docker compose up -d --build
```

Then check it:

```bash
curl http://localhost:8002/health
curl "http://localhost:8002/search/?s=creep&limit=3"
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
| `LUCIDA_SERVICE` | `qobuz` | lucida.to service to search. |
| `LUCIDA_COUNTRY` | `US` | Country lucida.to reports results for. |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8002` | Bind address. |
| `LUCIDA_USER_AGENT` | a Chrome UA | User agent used for lucida.to. |
| `LUCIDA_PROBE_QUERY` | `test` | Query used to attach a real track to the client's probe id. |
| `DOWNLOAD_DIR` | `/data/downloads` | Where downloaded audio is cached. Used verbatim when set. |
| `LUCIDADL_HOME` | `/data` | Cloudflare cookie + browser profile location. |

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
- **Search depends on lucida.to's own service health.** Amazon search currently
  fails with a backend `ENOENT` and Spotify is disabled upstream, which is why
  the default is `qobuz`. The proxy fails over between services, and returns
  **502 with the reason** when *no* service answers — a query that genuinely
  matched nothing still returns 200 with zero items.
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
