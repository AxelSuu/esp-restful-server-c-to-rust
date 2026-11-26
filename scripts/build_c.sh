#!/usr/bin/env bash
# Clean, reproducible C build for the ESP32-C6. Wi-Fi from WIFI_SSID / WIFI_PASSWORD.
set -euo pipefail
cd "$(dirname "$0")/../c"
sys_path=$PATH
while IFS== read -r k v; do export "$k=$v"; done < <(bash "$HOME/.espressif/tools/activate_idf_v6.0.sh" -e)
export PATH="$IDF_PATH/tools:$PATH:$sys_path"

printf 'CONFIG_EXAMPLE_WIFI_SSID="%s"\nCONFIG_EXAMPLE_WIFI_PASSWORD="%s"\n' \
    "${WIFI_SSID:?}" "${WIFI_PASSWORD:?}" > sdkconfig.wifi
[ -d build ] || rm -f sdkconfig
idf.py -DIDF_TARGET=esp32c6 -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.wifi" "${@:-build}"
