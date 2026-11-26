#!/usr/bin/env bash
# Release build for the ESP32-C6. Wi-Fi from WIFI_SSID / WIFI_PASSWORD.
set -euo pipefail
cd "$(dirname "$0")/../rust"
: "${WIFI_SSID:?}" "${WIFI_PASSWORD:?}"
cargo build --release "$@"
