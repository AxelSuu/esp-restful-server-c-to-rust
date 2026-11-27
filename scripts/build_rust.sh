#!/usr/bin/env bash
# Release build for the ESP32-C6. Wi-Fi from WIFI_SSID / WIFI_PASSWORD.
set -euo pipefail
# Wi-Fi credentials: environment, else the gitignored wifi.env at the repo root.
[ -n "${WIFI_SSID:-}" ] || [ ! -f "$(dirname "$0")/../wifi.env" ] || . "$(dirname "$0")/../wifi.env"
export WIFI_SSID WIFI_PASSWORD
cd "$(dirname "$0")/../rust"
: "${WIFI_SSID:?}" "${WIFI_PASSWORD:?}"
cargo build --release "$@"
