#!/usr/bin/with-contenv bashio
set -euo pipefail

export HOME_MAINTENANCE_DB_PATH="${HOME_MAINTENANCE_DB_PATH:-/data/home-maintenance.db}"
export HOME_MAINTENANCE_HOST="${HOME_MAINTENANCE_HOST:-0.0.0.0}"
export HOME_MAINTENANCE_PORT="${HOME_MAINTENANCE_PORT:-8099}"
export HOME_MAINTENANCE_UPCOMING_WINDOW_DAYS="$(bashio::config 'upcoming_window_days')"
export HOME_MAINTENANCE_LOG_REQUESTS="$(bashio::config 'request_logging')"
export HOME_MAINTENANCE_PUBLISH_HOMEASSISTANT="$(bashio::config 'publish_homeassistant_sensors')"
export HOME_MAINTENANCE_HA_SYNC_INTERVAL_SECONDS="$(bashio::config 'homeassistant_publish_interval_seconds')"
export HOME_MAINTENANCE_SEED_DEMO="$(bashio::config 'seed_demo_data')"

python3 /app/server.py
