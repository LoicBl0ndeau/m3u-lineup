#!/bin/sh
set -e

DATA_DIR="$(dirname "${DB_PATH:-/data/lineup.sqlite3}")"
mkdir -p "$DATA_DIR"
chown -R lineup:lineup "$DATA_DIR"

exec su-exec lineup "$@"