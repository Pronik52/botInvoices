#!/bin/sh
set -eu
cd "$(dirname "$0")"
# Run the container as the user who owns the persistent data directory.
export APP_UID="$(id -u)"
export APP_GID="$(id -g)"
umask 077
mkdir -p data
if [ ! -w data ]; then
    echo 'Папка data недоступна для записи пользователю chatgpt.' >&2
    exit 1
fi
chmod 700 data
if docker info >/dev/null 2>&1; then
    docker compose up -d --build bot
else
    sudo -n env APP_UID="$APP_UID" APP_GID="$APP_GID" docker compose up -d --build bot
fi
