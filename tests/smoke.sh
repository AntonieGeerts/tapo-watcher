#!/usr/bin/env bash
# End-to-end check without the real camera: the mock ONVIF service sends a motion event,
# the app grabs pictures from a generated test video and sends them to the Decisions API
# (a real API call, using decisionsapi.txt). The test video has no person in it, so the
# expected verdict is "no_person".
set -euo pipefail
cd "$(dirname "$0")/.."
TMP=$(mktemp -d)
PORT=8766

ffmpeg -hide_banner -loglevel error -f lavfi -i testsrc2=size=1280x720:rate=15 -t 12 -pix_fmt yuv420p "$TMP/sample.mp4"

.venv/bin/python -m tests.mock_camera >"$TMP/mock.log" 2>&1 &
MOCK=$!
CAMERA_HOST=127.0.0.1 ONVIF_PORT=18080 CAMERA_USER=mock CAMERA_PASS=mockpass \
  RTSP_URL="$TMP/sample.mp4" EVENTS_DIR="$TMP/events" WEB_PORT=$PORT SNAPSHOTS=2 TRIGGER_MODE=motion \
  .venv/bin/python -m app >"$TMP/app.log" 2>&1 &
APP=$!
trap 'kill $APP $MOCK 2>/dev/null || true; wait 2>/dev/null || true' EXIT

for _ in $(seq 60); do
  sleep 1
  if curl -sf "localhost:$PORT/api/events" | grep -qE '"status": ?"(done|error)"'; then break; fi
done

echo "== status";  curl -s "localhost:$PORT/api/status" | .venv/bin/python -m json.tool
echo "== events";  curl -s "localhost:$PORT/api/events" | .venv/bin/python -m json.tool
echo "== dashboard"; curl -s -o /dev/null -w "GET / -> %{http_code}\n" "localhost:$PORT/"
echo "== files";   ls -la "$TMP"/events/*/
echo "== app log"; cat "$TMP/app.log"
echo "== mock log"; cat "$TMP/mock.log"
