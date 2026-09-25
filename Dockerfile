# lucida.to -> HiFi API proxy.
#
# lucida.to sits behind Cloudflare, and lucidadl deliberately clears it with a
# HEADED Chromium (true headless is challenged and fails). A container has no
# display, so the browser runs under Xvfb. That is why this image installs xvfb
# and why the entrypoint is `xvfb-run` rather than plain `python`.
#
# xauth is required by xvfb-run itself (it creates an auth cookie for the display),
# and the xvfb package does not pull it in: without it the container exits 3 with
# "xvfb-run: error: xauth command not found" before Python ever starts.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Keep the Cloudflare cookie + browser profile + download cache on a volume.
    LUCIDADL_HOME=/data \
    DOWNLOAD_DIR=/data/downloads

WORKDIR /app

COPY requirements.txt ./
# --with-deps installs Chromium together with the shared libraries it needs.
RUN pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends xvfb xauth \
    && rm -rf /var/lib/apt/lists/*

COPY lucidadl/ ./lucidadl/
COPY lucida_hifi_proxy.py ./

RUN mkdir -p /data/downloads
VOLUME ["/data"]
EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8002/health', timeout=5)"

CMD ["xvfb-run", "-a", "--server-args=-screen 0 1366x900x24", "python", "lucida_hifi_proxy.py"]
