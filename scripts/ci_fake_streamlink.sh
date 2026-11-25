#!/usr/bin/env bash
# Stands in for streamlink in CI (STREAMLINK_BIN): an endless test picture with
# tone as MPEG-TS on stdout. Every argument is ignored.
exec ffmpeg -hide_banner -loglevel error -re \
    -f lavfi -i testsrc2=size=640x360:rate=30 -f lavfi -i sine=frequency=440:sample_rate=48000 \
    -c:v libx264 -preset ultrafast -tune zerolatency -pix_fmt yuv420p -g 60 -b:v 1500k \
    -c:a aac -b:a 128k -f mpegts pipe:1
