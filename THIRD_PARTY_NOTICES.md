# Third-party notices

## `lucidadl/` — vendored (partial copy), MIT

`lucidadl/` is a copy of [Jude-A/lucidadl](https://github.com/Jude-A/lucidadl),
version 1.4.0 (per `lucidadl/__init__.py`). It is **MIT licensed**, Copyright
(c) 2026 Jude-A and lucidadl contributors. The upstream license text is
reproduced at <lucidadl/LICENSE>, and is what permits redistribution here.

It is vendored rather than installed as a dependency because this proxy pins a
specific upstream version to keep the documented wire behaviour stable, and
because a pinned PyPI release would not include the one local block described
below (which the proxy no longer needs — see "Local modification").

Upstream license: <https://github.com/Jude-A/lucidadl/blob/main/LICENSE>

### Removed from the upstream package

Only the modules this proxy can actually reach are kept. Deleted upstream files:

`__main__.py`, `cli.py`, `downloader.py`, `matching.py`, `models.py`,
`organize.py`, `progress.py`, `transcode.py`, `tui.py`.

The proxy imports `lucidadl.api`, `lucidadl.utils` and `lucidadl.session`.
`api` needs `paths` and `utils`; `session` needs `paths`. Nothing kept refers to
anything removed, and the pruned tree was verified by importing the proxy
against it. Dropping the CLI also drops its dependencies — `click`, `rich`,
`questionary`, `mutagen`, `imageio-ffmpeg` — none of which `requirements.txt`
installs.

To restore the full package, copy the deleted files back from
<https://github.com/Jude-A/lucidadl> at version 1.4.0.

### No local modifications

An earlier revision carried one patch at the end of `search()` in
`lucidadl/api.py`, copying lucida.to's in-band `{"success": false, "error":
"..."}` into a `results["error"]` key so the proxy could tell "this search
failed" apart from "this search matched nothing".

**That patch has been removed.** `LucidaWrapper.search()` now issues the
`/search` request itself and decodes the SvelteKit data node directly
(`extract_data_node` / `flatten_search`), so it reads the in-band failure flag
from lucida's response without help from the patched branch. It also no longer
overrides `api.default_country`, because the accepted country is now discovered
from the `countries` member that rides on every response.

Consequences:

- `LucidaClient.search()` is unused by this project. The upstream copy is kept
  for the parts this proxy does use (`_get` with its Cloudflare refresh and
  retry, `fetch_page_data`, `tracks_from_pd`, `start_download`, `run_job`).
- **Updating `lucidadl/` is now a clean file copy** — there is nothing to
  re-apply, and no marker to grep for.
- A future refactor could rebase the vendored tree onto upstream at any time
  without behavioural risk to this proxy.

## `hifi-api` — MIT

<https://github.com/binimum/hifi-api>, MIT License, Copyright (c) 2023 sachin
senal (itself forked from <https://github.com/sachinsenal0x64/hifi>).

This project does not redistribute it. The proxy implements the same HTTP
surface — endpoint paths, query parameters and JSON response shapes — so that
clients already speaking to hifi-api can talk to this one instead. Response
shapes are interface facts; the credit is given because the interface was
designed by that project.

## SoulSync — compatibility target

<https://github.com/Nezreka/SoulSync>. Not redistributed. Its
`core/hifi_client.py` was read to determine which endpoints, parameters and
response fields a HiFi client actually depends on, so this proxy would be
compatible with it.

## lucida.to

<lucida.to>. A third-party service this project talks to. Not
affiliated with, endorsed by, or connected to this project.

## Python dependencies

`fastapi`, `uvicorn`, `httpx`, `pyjson5` and `playwright` are installed from
PyPI at build time and remain under their own licenses (MIT / BSD / Apache 2.0).
No dependency code is vendored here.
