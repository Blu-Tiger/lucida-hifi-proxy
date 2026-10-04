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

# A restart of the same container (docker restart, a killed container that is
# started again) leaves Xvfb's lock and socket behind: the old Xvfb was killed
# without a chance to clean up. The next Xvfb then refuses to start ("Server is
# already active for display 99"), the stale socket makes the wait loop below
# pass, and the proxy runs with DISPLAY pointing at no server at all - Chromium
# dies with Playwright's generic "Target page, context or browser has been
# closed", which names neither X nor the lock. Nothing else runs in this
# container before this point, so any lock found here is stale by definition.
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"

echo "entrypoint: starting Xvfb on ${DISPLAY} (${SCREEN})"
# -ac disables X access control, so no xauth cookie is needed for this display.
Xvfb "${DISPLAY}" -screen 0 "${SCREEN}" -ac -nolisten tcp &
XVFB_PID=$!

# Wait for the display socket, but never forever. If Xvfb dies, start the proxy
# anyway so the real error reaches the logs instead of an unbounded hang.
i=0
while [ ! -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; do
    if ! kill -0 "$XVFB_PID" 2>/dev/null; then
        echo "entrypoint: WARNING: Xvfb exited before creating ${DISPLAY}" >&2
        break
    fi
    i=$((i + 1))
    if [ "$i" -ge 50 ]; then
        echo "entrypoint: WARNING: Xvfb did not create ${DISPLAY} within 10s" >&2
        break
    fi
    sleep 0.2
done

echo "entrypoint: launching proxy"
exec python lucida_hifi_proxy.py
