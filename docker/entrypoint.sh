#!/bin/sh
# Starts the all-in-one container. supervisord stays root so it can drop
# privileges; every StreamVault program runs as the user "streamvault".
set -eu

for name in VOD_USER VOD_PASS VOD_SECRET_KEY; do
    eval "value=\${$name:-}"
    if [ -z "$value" ]; then
        echo "StreamVault: set $name (VOD_USER must not be \"admin\"; VOD_SECRET_KEY: openssl rand -hex 32)." >&2
        exit 64
    fi
done

case "$APP_UID" in
    '' | *[!0-9]* | 0)
        echo "StreamVault: APP_UID must be a number above 0." >&2
        exit 64
        ;;
esac
if [ "$(id -u streamvault)" != "$APP_UID" ]; then
    usermod --non-unique --uid "$APP_UID" streamvault
    chown -R streamvault:streamvault /home/streamvault
fi
# Only the top directories: archives can be large, and their files already
# belong to the same user on a second start.
install -d -o streamvault -g streamvault /app/data /app/.hls_live
exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
