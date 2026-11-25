#!/bin/sh
# Image build step: derive the single-container nginx and supervisor
# configuration from the files the Compose stack uses, so there is one source.
set -eu

sed -e 's#http://app:8080#http://127.0.0.1:8080#' \
    -e 's#http://app:8085#http://127.0.0.1:8085#' \
    -e 's#listen 80;#listen 80 default_server;#' \
    /app/nginx.conf > /etc/nginx/conf.d/streamvault.conf
if grep -q 'http://app:' /etc/nginx/conf.d/streamvault.conf; then
    echo "nginx.conf still points at the Compose host name" >&2
    exit 1
fi
ln -sf /dev/stdout /var/log/nginx/access.log
ln -sf /dev/stderr /var/log/nginx/error.log

mkdir -p /etc/supervisor/conf.d
sed -e 's#--bind 0.0.0.0:8080#--bind 127.0.0.1:8080#' /app/docker-worker.conf > /etc/supervisor/conf.d/supervisord.conf
grep -q -- '--bind 127.0.0.1:8080' /etc/supervisor/conf.d/supervisord.conf
cat >> /etc/supervisor/conf.d/supervisord.conf <<'EOF'

[program:nginx]
command=/usr/sbin/nginx -g "daemon off;"
autostart=true
autorestart=true
startsecs=5
stopsignal=QUIT
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0
EOF
nginx -t
