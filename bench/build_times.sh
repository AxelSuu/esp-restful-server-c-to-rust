#!/usr/bin/env bash
# Clean and incremental build times with hyperfine. Toolchains and downloaded components stay warm.
set -euo pipefail
cd "$(dirname "$0")/.."
export WIFI_SSID=${WIFI_SSID:-bench} WIFI_PASSWORD=${WIFI_PASSWORD:-benchbench}
mkdir -p results
RUNS=${RUNS:-3}

hyperfine --runs "$RUNS" --export-json results/build_clean.json \
    -n c --prepare 'rm -rf c/build' 'scripts/build_c.sh' \
    -n rust --prepare 'cargo clean -q --manifest-path rust/Cargo.toml' 'scripts/build_rust.sh'

hyperfine --runs "$RUNS" --warmup 1 --export-json results/build_incremental.json \
    -n c --prepare 'touch c/main/rest_server.c' 'scripts/build_c.sh' \
    -n rust --prepare 'touch rust/src/web.rs' 'scripts/build_rust.sh'
