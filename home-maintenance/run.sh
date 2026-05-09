#!/usr/bin/with-contenv bashio
set -euo pipefail

export HOME_MAINTENANCE_DB_PATH="${HOME_MAINTENANCE_DB_PATH:-/data/home-maintenance.db}"
export HOME_MAINTENANCE_HOST="${HOME_MAINTENANCE_HOST:-0.0.0.0}"
export HOME_MAINTENANCE_PORT="${HOME_MAINTENANCE_PORT:-8099}"
export HOME_MAINTENANCE_UPCOMING_WINDOW_DAYS="$(bashio::config 'upcoming_window_days')"
export HOME_MAINTENANCE_LOG_REQUESTS="$(bashio::config 'request_logging')"

python3 /app/server.py
