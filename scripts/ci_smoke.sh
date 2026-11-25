#!/usr/bin/env bash
# End-to-end smoke test for a running StreamVault stack, used by CI.
# Goes through the real nginx; no Twitch or YouTube traffic.
#
# Usage: SMOKE_USER=... SMOKE_PASS=... scripts/ci_smoke.sh http://localhost:8090
set -euo pipefail

BASE="${1:?base URL missing}"
: "${SMOKE_USER:?}" "${SMOKE_PASS:?}"
JAR="$(mktemp)"
trap 'rm -f "${JAR}"' EXIT

code() { curl -s -o /dev/null -w '%{http_code}' -b "${JAR}" "$@"; }

expect() {
    local label="$1" want="$2" got="$3"
    if [[ "${got}" != "${want}" ]]; then
        echo "FAIL ${label}: expected ${want}, got ${got}"
        exit 1
    fi
    echo "ok   ${label}: ${got}"
}

for _ in $(seq 1 60); do
    [[ "$(code "${BASE}/login" || true)" == "200" ]] && break
    sleep 2
done
expect "login page" 200 "$(code "${BASE}/login")"
expect "self-hosted fonts" 200 "$(code "${BASE}/static/fonts/fonts.css")"

# Direct live routes are closed without a session, internal ones always.
expect "live .ts without session" 401 "$(code "${BASE}/live/smoketest.ts")"
expect "live playlist without session" 401 "$(code "${BASE}/live/smoketest/stream.m3u8")"
expect "auth location is internal" 404 "$(code "${BASE}/_live_auth")"
expect "media location is internal" 404 "$(code "${BASE}/_protected/streamrecorder.db")"
expect "auth endpoint ignores a forged marker" 404 "$(code -H 'X-StreamVault-Live-Auth: 1' "${BASE}/api/live/auth")"

PAGE="$(curl -s -c "${JAR}" -b "${JAR}" "${BASE}/login")"
TOKEN="$(grep -o 'name="csrf_token" value="[^"]*"' <<<"${PAGE}" | head -n 1 | cut -d'"' -f4)"
[[ -n "${TOKEN}" ]] || { echo "FAIL no CSRF token on the login page"; exit 1; }
expect "login" 302 "$(curl -s -o /dev/null -w '%{http_code}' -c "${JAR}" -b "${JAR}" \
    --data-urlencode "csrf_token=${TOKEN}" \
    --data-urlencode "username=${SMOKE_USER}" \
    --data-urlencode "password=${SMOKE_PASS}" "${BASE}/login")"
expect "dashboard with session" 200 "$(code "${BASE}/")"
expect "dashboard without session" 302 "$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/")"

# The session passes auth_request; the livebot itself answers 404 because no
# watch page asked for this channel. 502 would mean that it is not running.
expect "live .ts reaches the livebot" 404 "$(code "${BASE}/live/smoketest.ts")"
echo "smoke test passed"
