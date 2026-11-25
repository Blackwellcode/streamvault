#!/usr/bin/env bash
# End-to-end proof of live playback for CI: browser request -> nginx ->
# auth_request -> livebot -> ffmpeg. The source is a local FFmpeg generator
# (scripts/ci_fake_streamlink.sh), so no Twitch traffic is involved.
#
# Usage: SMOKE_USER=... SMOKE_PASS=... scripts/ci_live_e2e.sh http://localhost:8090
set -euo pipefail

BASE="${1:?base URL missing}"
: "${SMOKE_USER:?}" "${SMOKE_PASS:?}"
CHANNEL=e2echannel
WORK="$(mktemp -d)"
JAR="${WORK}/jar"

PAGE="$(curl -s -c "${JAR}" "${BASE}/login")"
TOKEN="$(grep -o 'name="csrf_token" value="[^"]*"' <<<"${PAGE}" | head -n 1 | cut -d'"' -f4)"
curl -s -o /dev/null -c "${JAR}" -b "${JAR}" --data-urlencode "csrf_token=${TOKEN}" \
    --data-urlencode "username=${SMOKE_USER}" --data-urlencode "password=${SMOKE_PASS}" "${BASE}/login"

echo "1. The watch page polls until the livebot delivers"
READY=""
for _ in $(seq 1 40); do
    READY="$(curl -s -b "${JAR}" "${BASE}/api/live/ready/${CHANNEL}")"
    grep -q '"ready": *true' <<<"${READY}" && break
    sleep 1
done
grep -q '"ready": *true' <<<"${READY}" || { echo "FAIL never ready: ${READY}"; exit 1; }

echo "2. Real-time MPEG-TS through nginx"
curl -s -b "${JAR}" --max-time 8 -o "${WORK}/live.ts" "${BASE}/live/${CHANNEL}.ts" || true
ffprobe -v error -show_entries stream=codec_type,codec_name -of csv=p=0 "${WORK}/live.ts" | tee "${WORK}/ts.txt"
grep -q 'h264,video' "${WORK}/ts.txt" && grep -q 'aac,audio' "${WORK}/ts.txt"
test "$(stat -c %s "${WORK}/live.ts")" -gt 100000

echo "3. HLS playlist and segment through nginx"
curl -sf -b "${JAR}" -o "${WORK}/stream.m3u8" "${BASE}/live/${CHANNEL}/stream.m3u8"
head -n 12 "${WORK}/stream.m3u8"
SEGMENT="$(grep -v '^#' "${WORK}/stream.m3u8" | tail -n 1)"
curl -sf -b "${JAR}" -o "${WORK}/seg.ts" "${BASE}/live/${CHANNEL}/$(basename "${SEGMENT}")"
ffprobe -v error -show_entries stream=codec_type -of csv=p=0 "${WORK}/seg.ts" | grep -q video

echo "4. The WebUI playlist used by iPhones"
curl -sf -b "${JAR}" "${BASE}/live/playlist/${CHANNEL}" | grep -q '#EXTM3U'

echo "5. Nobody without a session gets the running stream"
test "$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/live/${CHANNEL}.ts")" = "401"
test "$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/live/${CHANNEL}/stream.m3u8")" = "401"
echo "live playback works end to end"
