# Third-party notices

## `lucidadl/` — vendored (partial copy), MIT

`lucidadl/` is a copy of [Jude-A/lucidadl](https://github.com/Jude-A/lucidadl),
version 1.4.0 (per `lucidadl/__init__.py`). It is **MIT licensed**, Copyright
(c) 2026 Jude-A and lucidadl contributors. The upstream license text is
reproduced at <lucidadl/LICENSE>, and is what permits redistribution here.

It is vendored rather than installed as a dependency because this proxy carries
one small patch inside `lucidadl/api.py` (see below), which a pinned PyPI
release would not include.

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

### Local modification

Exactly one block was added, at the end of `search()` in `lucidadl/api.py`. It
copies lucida.to's in-band `{"success": false, "error": "..."}` value into a
`results["error"]` key so the proxy can tell "this search failed" apart from
"this search matched nothing", which the stock code collapses into the same
empty result. The block is marked in the source with:

```
PATCH TO UPSTREAM lucidadl: re-apply after updating it.
```

Every file that remains is upstream and unmodified apart from that one block. If
you upgrade `lucidadl/`, re-apply it or search failure detection regresses to
silent empty results.

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
