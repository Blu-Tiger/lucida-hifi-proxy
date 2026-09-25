#!/bin/sh
# Start a virtual X display, then the proxy.
#
# This replaced `xvfb-run`, which hid the entire X layer. xvfb-run sends Xvfb's
# own error output to /dev/null by default, so a display that fails to come up
# looks identical to the container hanging silently with no logs — and its one
# visible failure mode was a bare "xauth command not found" that said nothing
# about the X server. Starting Xvfb here means every step is on stdout.
set -eu

SCREEN="${XVFB_SCREEN:-1366x900x24}"
DISPLAY_NUM="${XVFB_DISPLAY:-99}"
export DISPLAY=":${DISPLAY_NUM}"

echo "entrypoint: starting Xvfb on ${DISPLAY} (${SCREEN})"
# -ac disables X access control, so no xauth cookie is needed for this display.
Xvfb "${DISPLAY}" -screen 0 "${SCREEN}" -ac -nolisten tcp &

# Wait for the display socket, but never forever. If Xvfb dies, start the proxy
# anyway so the real error reaches the logs instead of an unbounded hang.
i=0
while [ ! -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; do
    i=$((i + 1))
    if [ "$i" -ge 50 ]; then
        echo "entrypoint: WARNING: Xvfb did not create ${DISPLAY} within 10s" >&2
        break
    fi
    sleep 0.2
done

echo "entrypoint: launching proxy"
exec python lucida_hifi_proxy.py
