#!/usr/bin/env bash
# C build for the ESP32-C6. Wi-Fi from WIFI_SSID / WIFI_PASSWORD; HEAP_STATS=1 adds /api/v1/heap.
set -euo pipefail
cd "$(dirname "$0")/../c"
sys_path=$PATH
while IFS== read -r k v; do export "$k=$v"; done < <(bash "$HOME/.espressif/tools/activate_idf_v6.0.sh" -e)
export PATH="$IDF_PATH/tools:$PATH:$sys_path"

local_cfg=$(printf 'CONFIG_EXAMPLE_WIFI_SSID="%s"\nCONFIG_EXAMPLE_WIFI_PASSWORD="%s"\nCONFIG_EXAMPLE_HEAP_STATS=%s' \
    "${WIFI_SSID:?}" "${WIFI_PASSWORD:?}" "$([ "${HEAP_STATS:-0}" = 1 ] && echo y || echo n)")
# Rewrite only on change: a new mtime forces a full CMake reconfigure, and defaults only
# reach a fresh sdkconfig.
if [ "$(cat sdkconfig.local 2>/dev/null)" != "$local_cfg" ]; then
    echo "$local_cfg" > sdkconfig.local
    rm -f sdkconfig
fi
idf.py -DIDF_TARGET=esp32c6 -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.local" "${@:-build}"
