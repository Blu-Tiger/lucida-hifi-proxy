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
# xauth is not needed by entrypoint.sh (Xvfb runs with -ac), but it is kept
# deliberately: the xvfb package does not depend on it, and its absence was
# previously an opaque "xauth command not found" that killed the container
# before Python started. It costs ~200 KB to rule that class of failure out.
RUN pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends xvfb xauth \
    && rm -rf /var/lib/apt/lists/*

COPY lucidadl/ ./lucidadl/
COPY lucida_hifi_proxy.py entrypoint.sh ./

RUN mkdir -p /data/downloads
VOLUME ["/data"]
EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8002/health', timeout=5)"

CMD ["sh", "entrypoint.sh"]
