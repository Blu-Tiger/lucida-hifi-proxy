# lucida.to -> HiFi API proxy.
#
# lucida.to sits behind Cloudflare, and lucidadl deliberately clears it with a
# HEADED Chromium (true headless is challenged and fails). A container has no
# display, so the browser runs under Xvfb, started by entrypoint.sh rather than
# by `xvfb-run` — see that file for why the wrapper was removed.
FROM python:3.12-slim-bookworm

# Keep the Cloudflare cookie + browser profile + download cache on a volume.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LUCIDADL_HOME=/data \
    DOWNLOAD_DIR=/data/downloads

WORKDIR /app

COPY requirements.txt ./
# --with-deps installs Chromium together with the shared libraries it needs.
#
# --no-shell skips Playwright's chromium-headless-shell (~120 MB compressed).
# That build only serves true-headless launches, and this image deliberately
# never does one: Cloudflare challenges headless, so every launch is headed
# under Xvfb (see entrypoint.sh). Without the flag the shell is downloaded into
# the image and never executed.
#
# xauth is not needed by entrypoint.sh (Xvfb runs with -ac), but it is kept
# deliberately: the xvfb package does not depend on it, and its absence was
# previously an opaque "xauth command not found" that killed the container
# before Python started. It costs ~200 KB to rule that class of failure out.
#
# One RUN, so apt's package lists and caches land in the same layer and are
# removed by the same cleanup step; splitting them would freeze them into an
# earlier layer that rm can only hide, not shrink. pip's cache is already off
# via PIP_NO_CACHE_DIR above. /root/.cache is NOT deleted: Playwright installs
# Chromium under /root/.cache/ms-playwright, so removing it ships an image
# whose browser is missing (it did exactly that once).
RUN pip install -r requirements.txt \
    && python -m playwright install --with-deps --no-shell chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends xvfb xauth \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/*

COPY lucidadl/ ./lucidadl/
COPY lucida_hifi_proxy.py entrypoint.sh ./

RUN mkdir -p /data/downloads
VOLUME ["/data"]
EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8002/health', timeout=5)"

CMD ["sh", "entrypoint.sh"]
